# Edge-Cloud Vision System

这是一个可以在一台普通 PC 上完整跑通的边缘视觉参考项目：端侧先做一次轻量判断，只把结构化 JSON 发出去；云端只处理“拿不准”的事件；图像本身不出设备。

它不是一个识别精度排行榜项目，也不是工业产品。它更适合拿来学习或做课程设计：看清楚采集、推理、门限、通信、云端兜底、测量和时间戳是怎么接起来的。

当前版本 v0.6.2。

v0.6.1 给标定脚本加了 `beta` 参数（小标定集下的三参数校准），给门限拟合加了 `--risk-control wilson`（用单侧置信上界而不是观测 FAR 对照预算）。v0.6.2 是收口版本：保留这些标定/风险控制，修掉 Windows 串口回放的 UTF-8 BOM 兼容问题，并把端到端 smoke 记录写进文档。没有改运行时识别逻辑。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-95%20passed-brightgreen.svg)](tests)
[![Release](https://img.shields.io/badge/release-v0.6.2-blue.svg)](https://github.com/RyeSama/edge-cloud-vision/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 版本演进

| 版本 | 一句话 | 主要变化 |
|---|---|---|
| v0.1 | 先把链路跑通 | 端侧 TFLite 门限、MQTT QoS1、FastAPI + LLM 兜底 |
| v0.2 | 让系统能“记住” | Edge-Mem：SQLite 三层存储、时间窗 + 向量检索、RRF、Token 预算 |
| v0.3 | 让系统能“证明” | 采样时间链、延迟/抖动/截止期、置信度标定、Modbus TCP 联锁、SPC、激励台 |
| v0.4 | 让门限可验收 | 温度标定、按 false-accept 预算分条件选 accept 门限、独立测试集 |
| v0.5 | 从“识别”走到“测量” | 棋盘格标定、平面位姿、像素→毫米、蒙特卡洛不确定度、Gauge R&R |
| v0.6 | 让第一次做毕设的人能照着跑 | 串口 NDJSON、ESP32 固件、`serial_monitor`、`/dashboard` 上位机、80 个测试 |
| v0.6.1 | 把标定和风险控制补稳 | `beta` 标定、`--risk-control wilson` 有限样本上界、95 个测试 |
| v0.6.2 | 收口串口和全链路 | 串口回放兼容 UTF-8 BOM、端到端 smoke 记录、95 个测试 |

v0.5.0 的重点不是多一个功能，而是把“图里有什么”变成“两个点之间多少毫米、这个数有多不确定”。它只适用于与棋盘格共面的目标；合成验证不能当真实镜头精度。

---

## 先选一条路

不用一上来就接硬件。按你的目标选一条：

| 路线 | 你需要 | 能看到什么 | 大概时间 |
|---|---|---|---|
| A. 纯软件 | 一台电脑 | 端侧推理、JSON、门限、云端兜底、上位机页面 | 10 分钟 |
| B. 接摄像头 | 电脑 + USB 摄像头 | 实时画面、识别结果、延迟 | 20 分钟 |
| C. 接板子 | 电脑 + ESP32 + 两个传感器 | 板子采样、串口 JSON、PC 端表格 | 1–2 小时 |

第一次跑，建议先走 A。A 跑通再碰摄像头和板子，出问题容易定位。

---

## A. 10 分钟纯软件跑通

### 1. 装 Python 环境

要求 Python 3.10 或更高。Windows PowerShell：

```powershell
cd edge-cloud-vision
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install -U pip
pip install -e ".[litert,dev]"
python scripts/download_model.py
```

Linux / macOS 把激活命令换成 `source .venv/bin/activate`。

`download_model.py` 会下载 MobileNetV1 INT8 模型（约 4.3 MB）和 1000 行 ImageNet 标签。模型不进 Git 仓库，所以第一次必须跑。

> 没有摄像头也能跑。项目自带 `synthetic` 合成源，画面是一个移动方块，用来验证链路，不是用来评估识别精度。

### 2. 先不接 broker，跑 10 帧

```bash
python -m ecvision.edge --source synthetic --max-frames 10 --dry-run
```

这条命令只做：采集 → 预处理 → TFLite 推理 → 门限 → 打印 JSON。不连 MQTT、不需要摄像头、不需要 API Key。

看到一行行 JSON 就说明端侧链路通了。

### 3. 起云端网关和上位机

开一个终端：

```bash
python -m ecvision.cloud
```

再开一个终端：

```bash
python -m ecvision.edge --source synthetic --max-frames 100
```

浏览器打开：

```text
http://127.0.0.1:8000/dashboard
```

这就是上位机页面。你会看到：

- 顶部卡片：MQTT 连接状态、事件数、队列深度、LLM 调用/降级次数、Edge-Mem 统计；
- 中间表格：每帧的时间、设备、标签、置信度、门限结果、端到端延迟、云端决策；
- 页面每秒自动刷新，不需要手点。

如果表格一直是“等待事件”，先看 `/healthz`：

```bash
curl http://127.0.0.1:8000/healthz
```

`mqtt.connected` 是 `false`，说明 broker 没起，或者端口不对。

### 4. 起本地 MQTT broker

有 Docker：

```bash
docker compose up -d mosquitto
```

没有 Docker：装 Mosquitto，用 `deploy/mosquitto/mosquitto.conf` 启动，端口 1883。

Windows 上如果 `localhost` 解析到 IPv6 导致连不上，把 `.env` 里的 `ECV_MQTT_HOST` 改成 `127.0.0.1`。

### 5. 没有 LLM Key 会怎样

不会崩。所有 `review` 事件走 `rule_fallback`，返回：

```json
{"risk_level":"unknown","action":"request_human_review","source":"rule_fallback"}
```

`/healthz` 里 `llm_last_error` 会写原因。这是故意的降级路径，不是 bug。

---

## B. 接 USB 摄像头

Windows：

```bash
python -m ecvision.edge --source 0 --preview --max-frames 300
```

Linux：

```bash
python -m ecvision.edge --source /dev/video0 --preview --max-frames 300
```

`--preview` 会弹一个 OpenCV 窗口。按 `q` 退出。

如果摄像头打不开：

1. 先确认系统相机 App 能看到画面；
2. Windows 换一个索引试：`--source 1`；
3. 关掉占用摄像头的软件（微信、钉钉、OBS）；
4. 仍然不行就用 `--source synthetic` 先验证代码，不要卡在硬件上。

摄像头画面不会上传。端侧只发结构化字段，图像在本地处理完就丢。

---

## C. 接 ESP32 板子

这一节是给“毕设想接硬件、但以前没怎么摸过单片机”的人写的。照着做，不需要先学完 FreeRTOS。

### 1. 你需要什么

- ESP32 开发板一块（ESP32-WROOM-32 即可，NodeMCU-32S 也行）；
- BH1750 光照模块；
- HC-SR04 超声波模块；
- 一个按键、一个 LED、两个电阻（LED 限流用，1 kΩ 左右）；
- 面包板和杜邦线；
- 电脑装 Arduino IDE 2.x。

总成本大概几十块钱。

### 2. 接线

| 模块 | 模块引脚 | ESP32 引脚 | 说明 |
|---|---|---|---|
| BH1750 | VCC | 3V3 | 不要接 5V |
| BH1750 | GND | GND | 共地 |
| BH1750 | SDA | GPIO21 | I2C 数据 |
| BH1750 | SCL | GPIO22 | I2C 时钟 |
| HC-SR04 | VCC | 5V（VIN） | 用 5V，测距更稳 |
| HC-SR04 | GND | GND | 共地 |
| HC-SR04 | TRIG | GPIO5 | 触发 |
| HC-SR04 | ECHO | GPIO18 | 回响；5V 输出，建议 1k/2k 分压 |
| 按键 | 一端 | GPIO4 | 另一端接 GND，内部上拉 |
| LED | 正极 | GPIO2 | 串 1 kΩ 到 GND |

> HC-SR04 的 ECHO 是 5V 输出。大多数 ESP32 开发板 GPIO 不耐 5V。稳妥做法是加一个 1 kΩ / 2 kΩ 分压，或者用 3.3V 兼容的超声波模块。只是做课程演示、短时间测试，很多人直接接也能跑，但不要长期这么用。

### 3. 装 Arduino 环境

1. 打开 Arduino IDE；
2. `File → Preferences → Additional boards manager URLs` 填：

```text
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

3. `Tools → Board → Boards Manager` 搜 `esp32`，安装 `esp32 by Espressif Systems`；
4. `Tools → Manage Libraries` 搜 `BH1750`，安装 Christopher Laws 的 `BH1750`；
5. `Tools → Board` 选 `ESP32 Dev Module`；
6. 插上板子，`Tools → Port` 选出现的 COM 口（Windows）或 `/dev/ttyUSB0`（Linux）。

### 4. 烧录

打开：

```text
firmware/esp32_sensor/esp32_sensor.ino
```

点左上角“上传”。如果一直停在 `Connecting...`，按住板子上的 BOOT 键，看到 `Writing at...` 再松开。

烧录完成后打开串口监视器：

- 波特率选 `115200`；
- 会看到一行行 JSON，大约每秒 10 行。

示例：

```json
{"schema_version":"1.0","kind":"sensor","device_id":"esp32-01","boot_id":"a1b2c3d4","seq":42,"t_us":12345678,"lux":512.3,"distance_mm":803.0,"trigger":false}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `boot_id` | 每次重启随机生成，用来识别“板子重启了” |
| `seq` | 本次上电后的序号，从 0 开始，用来发现丢行 |
| `t_us` | 板子上电后的微秒数，单调递增；不是 Unix 时间 |
| `lux` | BH1750 光照，读不到时是 `null` |
| `distance_mm` | HC-SR04 距离，超时是 `null` |
| `trigger` | 按键按下为 `true` |

### 5. 在 PC 上看板子的数据

不接板子，先用回放文件看效果：

```bash
pip install -e ".[serial]"
python scripts/serial_monitor.py --replay docs/examples/serial-lines.jsonl
```

接真实板子：

```bash
python scripts/serial_monitor.py --port COM5 --baud 115200
```

Linux 把 `COM5` 换成 `/dev/ttyUSB0`。macOS 一般是 `/dev/cu.usbserial-*`。

你会看到：

```text
source: COM5 @ 115200
  t_s    seq   lux     distance_mm  trigger  note
  -----  ----  ------  -----------  -------  ----
  1.000     0   512.3        803.0        -  -
  1.100     1   509.8        802.0        -  -
  1.200     2   507.1        801.0        -  -
  1.300     3   505.6        800.0        -  -
```

如果中间少了一行，会打印 `MISSING n`。如果板子重启，会打印 `reboot`，序号从 0 重新开始。

### 6. 串口协议到底怎么定

板子和 PC 之间只传 **一行一个 JSON**（NDJSON），波特率 115200，8 数据位、无校验、1 停止位（8N1）。

为什么不用二进制帧：

- JSON 肉眼能看，串口监视器里直接调试；
- 一行一条，天然分帧，不需要找包头包尾；
- 对 10 Hz 的传感器数据，文本开销可以接受。

为什么没有 CRC：

- 这是课程/实验级链路，USB 串口本身误码率很低；
- 先让协议可读、可调试；
- 真上工业现场，应该加 CRC、序列号窗口和超时重传，或者直接上 Modbus RTU。

完整字段、错误处理和扩展约定见 [`docs/serial-protocol.md`](docs/serial-protocol.md)。

---

## 上位机页面到底显示什么

打开 `http://127.0.0.1:8000/dashboard`，页面不依赖前端框架，就是一个 FastAPI 返回的 HTML，每秒拉一次 `/healthz` 和 `/api/v1/events`。

| 区域 | 显示 | 用来判断 |
|---|---|---|
| 顶部卡片 | MQTT 连接、事件总数、队列深度、LLM/降级次数、Edge-Mem 条数 | 链路是否活着 |
| 事件表 | 时间、设备、标签、置信度、门限、端到端延迟、决策 | 识别和兜底是否正常 |
| 空状态 | “等待事件” | 网关起了但端侧没发，或 broker 没通 |

它故意做得很朴素：没有登录、没有数据库、没有 WebSocket。毕设演示要的是“能看见链路”，不是再写一个前端项目。

---

## 代码分别跑在哪

| 代码 | 跑在哪 | 作用 |
|---|---|---|
| `firmware/esp32_sensor/esp32_sensor.ino` | ESP32 板子 | 采光照、距离、按键，串口发 JSON |
| `scripts/serial_monitor.py` | PC | 读串口/回放，检查丢行和重启 |
| `src/ecvision/edge/` | PC（模拟 MCU） | 采集、预处理、TFLite 推理、门限、时间戳 |
| `src/ecvision/transport/` | PC | MQTT 发布、串口 JSON 解析 |
| `src/ecvision/cloud/` | PC（模拟边缘网关） | FastAPI、去重、LLM 兜底、Edge-Mem、上位机 |
| `src/ecvision/memory/` | PC | SQLite 三层存储、时间+语义检索 |
| `src/ecvision/metrology/` | PC | 相机标定、平面测量、不确定度 |

板子只负责采样，不跑视觉模型。MobileNetV1 INT8 约 4.3 MB，ESP32 的 RAM 和算力都不适合直接跑；硬要部署，只能换更小的模型和输入尺寸，那是另一个项目。

---

## 收到一条事件之后发生什么

以一次 `review` 为例：

1. 端侧读一帧，记下采集时间；
2. 预处理成 224×224，送 INT8 MobileNet；
3. 得到 top-1 和置信度；
4. `>= 0.90` 直接 accept；`0.50–0.90` 标 `ambiguous`；`< 0.50` 标 `low_confidence`；
5. 组 JSON，通过 MQTT QoS1 发到 `ecv/v1/{device_id}/event`；
6. 云端 Pydantic 校验、按 `event_id` 去重；
7. accept 只记录；review 调 LLM；
8. LLM 没 Key、超时、返回非 JSON，都降级成 `rule_fallback`；
9. 结果写进即时视图和 Edge-Mem；
10. 上位机每秒刷新。

原始图像在第 2 步之后就不再使用。

---

## 为什么低置信度不直接丢

工业场景里，“没看见”通常比“多调用一次大模型”更贵。

所以 `< 0.50` 不静默丢弃，而是标记 `low_confidence` 进入复核。这个策略有单元测试覆盖。你可以不同意这个取舍，但代码里它是显式的，不是漏写。

---

## 实测数据

### 带宽

1000 帧合成源，320×240，15 FPS：

| 指标 | 数值 |
|---|---:|
| JSON 总字节 | 563,944 B |
| 原始 RGB 基线 | 230,400,000 B |
| 相对原始 RGB 降幅 | 99.7552% |
| 相对 15 KB/帧 MJPEG 降幅 | 96.2404% |

只统计 MQTT payload，不含 TCP/TLS/MQTT 固定头。合成源对 ImageNet 模型是分布外输入，所以 1000 帧全部 `review`；这组数据验证的是带宽和兜底压力，不是 accept 比例。

### 时间链

200 帧，NTP，+50 ms 偏移，80 ms 截止期：

| 指标 | 数值 |
|---|---:|
| capture → publish p50 / p95 / p99 | 8.825 / 12.183 / 21.750 ms |
| 推理 p50 / p95 | 8.46 / 11.90 ms |
| 截止期错失 | 0 / 200 |
| 丢帧 | 0 |

### 平面测量

合成针孔相机，15 个棋盘格视图，12 次重复：

| 指标 | 数值 |
|---|---:|
| 标定 RMS 重投影 | 0.1384 px |
| 100 mm 真值测得 | 100.0899 mm |
| 12 次误差 RMS | 0.0696 mm |
| 扩展不确定度 U(k=2) | 1.2369 mm |

这是几何链路的合成验证，不是真实镜头精度。真实项目要用卡尺/量块对照，并重新标定。

---

## 目录结构

```text
edge-cloud-vision/
├─ src/ecvision/
│  ├─ contracts.py          # Event / Decision / SensorReading 契约
│  ├─ edge/                 # 采集、预处理、推理、门限、指标、管线
│  ├─ transport/            # MQTT、串口 JSON、topic
│  ├─ cloud/                # FastAPI、订阅、LLM 兜底、上位机
│  ├─ memory/               # Edge-Mem：SQLite、向量、时间窗、RRF
│  ├─ instrument/           # 时钟、标定、conformal、SPC、验收策略
│  ├─ metrology/            # 相机标定、平面测量、不确定度、Gauge
│  └─ control/              # Modbus TCP 从站、联锁状态机
├─ firmware/esp32_sensor/   # ESP32 固件和接线说明
├─ scripts/                 # 下模型、串口、标定、验收、测量、微基准
├─ examples/minimal_loop.py # 一屏版端侧骨架
├─ contracts/               # JSON Schema
├─ docs/                    # 协议、架构、串口、测量、实测数据
├─ tests/                   # 95 个测试
├─ docker-compose.yml
└─ pyproject.toml
```

---

## 测试

```bash
pytest -q
ruff check src tests scripts
python -m compileall -q src tests
```

当前 95 个测试，覆盖门限边界、契约、预处理、管线、云端降级、API 幂等、Edge-Mem 时间窗/RRF/Token 截断、时钟偏移、ECE/Brier、conformal、SPC、Modbus 往返、联锁、事件时间、激励台、相机标定、平面测量、不确定度、Gauge R&R、串口解析和上位机页面。GitHub Actions 在 Python 3.10 和 3.12 上跑 `ruff check` 和 `pytest -q`。

---

## 限制

模型和数据这块，MobileNetV1 是通用 ImageNet 模型，不是工业缺陷或安全场景专用模型；这组数字用来验证方法，不是识别精度。v0.4 每条件只有 8 张标定、9 张测试，门限方差很大，换数据要重新标定。

控制和保护这块，Modbus TCP 是纯标准库模拟从站，不是安全 PLC；联锁逻辑可以验证，但不能替代硬件安全回路。SPC 是诊断信号，不是自动停线依据。设备鉴权还没做，MQTT 用户名密码/TLS 只留了配置位。

测量这块，v0.5 只支持与棋盘格共面的目标；不确定度里的靶面平整度是假设值；合成相机验证的是几何和传播，不是真实镜头精度。

硬件这块，ESP32 固件是课程/实验级参考，没有做防水、防雷、隔离、看门狗和长期稳定性；HC-SR04 的 5V ECHO 需要分压。真上现场，请按工业规范重做。

---

## 常见问题

**没有摄像头能跑吗？**

能。`--source synthetic` 是确定性合成源，不需要摄像头、视频文件或 broker；`--dry-run` 只打印 JSON。

**没有 LLM Key 会怎样？**

云端照常启动，review 事件走 `rule_fallback`，返回 `unknown / request_human_review`。`/healthz` 里 `llm_enabled=false`。

**端口 8000 被占用？**

改环境变量 `ECV_API_PORT=8010`，或者关掉占用端口的程序。上位机地址跟着端口变。

**串口监视器打不开 COM 口？**

先关掉 Arduino 串口监视器，一个串口不能被两个程序同时打开。Linux 检查当前用户是否在 `dialout` 组。

**板子发数据，但 PC 收不到？**

先确认波特率 115200、接线 TX/RX 没反、共地；再用 Arduino 串口监视器看板子有没有输出。

**这个项目能直接上产线吗？**

不能。它是参考实现和测试台，不是产线交付物。上产线至少还需要设备鉴权、TLS、模型现场标定、异常恢复演练、安全评估和长期漂移监控。

---

## 文档

- [串口协议](docs/serial-protocol.md)
- [ESP32 固件说明](firmware/esp32_sensor/README.md)
- [架构与依赖方向](docs/architecture.md)
- [MQTT 协议](docs/protocol.md)
- [仪器化：时间链、标定、SPC、Modbus、激励台](docs/instrumentation.md)
- [平面视觉测量：像素到毫米、不确定度、Gauge R&R](docs/metrology.md)
- [v0.4 分条件验收数据](docs/measurements/v0.4/README.md)
- [v0.5 平面测量验证](docs/measurements/v0.5/README.md)
- [文档索引](docs/README.md)

---

## License

MIT。见 [LICENSE](LICENSE)。
