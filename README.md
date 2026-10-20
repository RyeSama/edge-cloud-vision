# Edge-Cloud Vision System

端侧用 TFLite 跑一个轻量模型，把识别结果通过 MQTT 发给网关；网关收到后，高置信度直接记录，拿不准的才调用 LLM。图像不出设备。

这个项目最早是想验证一个很具体的问题：在算力和带宽都受限的场景里，能不能让端侧先做一次便宜的判断，只把少量结构化结果送上云，而不是把视频一直往上推。现在的代码是 Python 参考实现，重点在结构、时序和测量，不在识别精度。当前版本 v0.5.0。

做到后面发现，识别本身不是最难的。更难的是回答：这条结果是什么时候产生的、延迟到底是多少、置信度能不能当概率用、出错时系统往哪边倒。v0.3 到 v0.5 基本都在补这些问题。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-72%20passed-brightgreen.svg)](tests)
[![Release](https://img.shields.io/badge/release-v0.5.0-blue.svg)](https://github.com/RyeSama/edge-cloud-vision/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 现在能做什么

端侧支持摄像头、视频文件和合成源，统一采集 320×240，跑 MobileNetV1 INT8。置信度 `>= 0.90` 直接接受，`0.50–0.90` 标成 `ambiguous`，`< 0.50` 标成 `low_confidence`。后两档都会进入复核，不会静默丢掉。

采集、推理、门限、发布各阶段都有时间戳。端侧输出的事件大概长这样：

```json
{
  "schema_version": "1.1",
  "event_id": "uuid4",
  "device_id": "edge-cam-01",
  "seq": 42,
  "ts": "2026-10-19T09:30:00.123+00:00",
  "source": {"width": 320, "height": 240, "fps": 15.0},
  "inference": {
    "model": "mobilenet_v1_1.0_224_quant",
    "label": "laptop",
    "label_id": 620,
    "confidence": 0.62,
    "latency_ms": 19.7,
    "top_k": [{"label": "laptop", "confidence": 0.62}]
  },
  "gate": {
    "decision": "review",
    "reason": "ambiguous",
    "accept_threshold": 0.9,
    "review_threshold": 0.5
  },
  "sample": {
    "capture_mono_ns": 1234567890,
    "capture_wall_ns": 1760866200123000000,
    "end_to_end_ms": 21.4,
    "deadline_ms": 66.7,
    "deadline_missed": false,
    "clock": {"source": "ntp", "boot_id": "a1b2c3", "offset_ms": 1.2, "synced": true}
  }
}
```

通信层用 MQTT 3.1.1 + QoS 1。事件走 `ecv/v1/{device_id}/event`，状态走 `status`，`status` 是 retained 的，带 LWT 和心跳。断线时端侧先缓冲，恢复后补发；云端按 `event_id` 去重。

网关是 FastAPI。高置信度事件只记录，需要复核的才调用 OpenAI 兼容接口。LLM 没配、超时、返回非法 JSON，都会落到 `rule_fallback`，返回 `unknown + request_human_review`，主流程不会崩。

事件会落进 Edge-Mem。SQLite 按时间分三层：10 分钟内保留明细，10 分钟到 2 小时聚合成摘要，2 小时以上只留极简索引。查询时先用时间窗过滤，再在候选集里做向量检索，用 RRF 合并时间排名和语义排名。向量库不可用时回退 SQLite 文本查询，没有 LLM 时返回 JSON 时间轴。

v0.3 加了采样时间链、置信度标定、SPC 漂移监控、确定性激励台和 Modbus TCP 联锁。v0.4 把标定和验收接在一起：按 false-accept 预算，为 clean / blur / occlusion / lowlight / downscale 分别选 accept 门限。离线策略和运行时门限是分开的，后者目前仍是单一阈值。

v0.5 加了一条平面几何测量链：棋盘格标定、`solvePnP` 平面位姿、像素反投影、毫米距离、蒙特卡洛不确定度和 Gauge R&R。它测的是“两个点之间多少毫米”，不是“图里像什么”。

## 怎么跑

### 1. 装环境

要求 Python 3.10+。Windows 上 PyPI 没有 `tflite-runtime` 的 wheel，用 `ai-edge-litert`：

```powershell
cd edge-cloud-vision
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[litert,dev]"
```

Linux / macOS 可以把 `litert` 换成 `tflite-runtime`，或者同样用 `ai-edge-litert`：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install tflite-runtime
```

### 2. 下模型

```bash
python scripts/download_model.py
```

脚本会校验 SHA-256，解出 `models/mobilenet_v1_1.0_224_quant.tflite`，并生成 1001 行 labels，第 0 类补 `background`。下载包约 35 MB，解压后的 `.tflite` 约 4.3 MB，不进仓库。

### 3. 先跑端侧

不接摄像头、不接 broker，先确认推理和门限能跑通：

```bash
python -m ecvision.edge --source synthetic --max-frames 10 --dry-run
```

接摄像头：

```bash
python -m ecvision.edge --source 0 --preview
```

跑视频文件：

```bash
python -m ecvision.edge --source path/to/your-video.mp4 --loop
```

只想看端侧最小代码，可以看 [`examples/minimal_loop.py`](examples/minimal_loop.py)。大约 50 行，采集、推理、门限、JSON，不依赖 MQTT 和指标模块。

### 4. 起 broker 和网关

```bash
docker compose up -d mosquitto
cp .env.example .env
python -m ecvision.cloud
```

网关默认在 `http://127.0.0.1:8000`，接口文档在 `/docs`。`.env` 里不填 `ECV_LLM_API_KEY` 也能启动，review 事件会走 `rule_fallback`。

没有 Docker 的话，可以装 Mosquitto，然后用 [`deploy/mosquitto/mosquitto.conf`](deploy/mosquitto/mosquitto.conf)。

### 5. 看一条事件

再开一个终端：

```bash
python -m ecvision.edge --source synthetic --max-frames 60 --metrics-out data/metrics.json
```

然后：

```bash
curl http://127.0.0.1:8000/healthz
curl "http://127.0.0.1:8000/api/v1/events?limit=5"
cat data/metrics.json
```

### 6. 试一下记忆

```bash
curl -X POST http://127.0.0.1:8000/api/v1/events \
  -H 'Content-Type: application/json' \
  -d @docs/examples/review-event.json

curl -X POST http://127.0.0.1:8000/api/v1/memory/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"过去 1 小时有哪些人？","limit":5,"format":"markdown"}'
```

`/api/v1/memory/ask` 在没配 LLM 时返回 `mode="json_timeline"`，配了 LLM 才返回自然语言回答。

### 7. 试一下 v0.3 / v0.4 / v0.5

时间链和截止期：

```bash
python -m ecvision.edge --source synthetic --max-frames 200 \
  --clock-source ntp --clock-offset-ms 50 --deadline-ms 80 \
  --metrics-out data/clock.json
```

Modbus 联锁：

```bash
python -m ecvision.edge --source synthetic --max-frames 300 \
  --interlock --modbus --modbus-port 15020
```

分条件验收的完整流程在 [`docs/measurements/v0.4/README.md`](docs/measurements/v0.4/README.md)。简单说就是：先切标定/测试集，再跑 `eval_dataset.py`，用 `fit_calibration.py` 拟合温度，用 `fit_policy.py` 选门限，最后在测试集上再跑一遍 `eval_dataset.py --policy`。

平面测量：

```bash
# 合成验证：标定、测距、不确定度、Gauge R&R 一次跑完
python scripts/metrology_demo.py --out docs/measurements/v0.5

# 真实棋盘格标定
python scripts/calibrate_camera.py \
  --images "data/calib/*.jpg" --cols 9 --rows 6 --square-mm 25 \
  --out data/camera.json

# 在同一平面上量两个像素之间的距离
python scripts/measure_checkerboard.py \
  --camera data/camera.json --image data/measure/scene.jpg \
  --a 412,308 --b 690,312
```

`measure_checkerboard.py` 假设被测对象和棋盘格共面；不共面时这个数没有意义。

## 结构

```text
edge-cloud-vision/
├─ src/ecvision/
│  ├─ contracts.py          # Event / Decision 契约
│  ├─ edge/                 # 采集、预处理、TFLite、门限、指标、管线
│  ├─ transport/            # MQTT 发布端、topic、离线缓冲
│  ├─ cloud/                # FastAPI、订阅、LLM 兜底、事件库
│  ├─ memory/               # Edge-Mem：SQLite、时间解析、RRF、上下文
│  ├─ instrument/           # 时钟、标定、conformal、SPC、验收策略
│  ├─ metrology/            # 相机标定、平面测量、不确定度、Gauge R&R
│  ├─ control/              # Modbus TCP 从站、联锁状态机
│  └─ bench/                # 确定性激励矩阵
├─ examples/minimal_loop.py # 一屏版端侧骨架
├─ scripts/                 # 下模型、标定、切分、验收、测量、微基准
├─ contracts/               # JSON Schema
├─ deploy/mosquitto/        # 本地 broker 配置
├─ docs/                    # 协议、架构、ADR、Edge-Mem、仪器化、测量、实测数据
├─ tests/                   # 72 个测试
├─ docker-compose.yml
└─ pyproject.toml
```

端侧和云端互不 import，只共享 `contracts.py` 和 `transport`。`EdgeRunner` 依赖 `FrameSource`、`Classifier`、`EventPublisher` 三个 Protocol，不直接依赖 OpenCV、TFLite 或 paho。这样换 C++/MCU 实现时，不用把整条链路重写。

## 一些取舍

通信层用 MQTT，而不是裸 Socket。JSON 事件很小，MQTT 的固定头开销可以忽略；真正麻烦的是重连、心跳、LWT、离线队列和 ACL。这些交给 broker 之后，端侧和云端代码能薄很多。Socket 只作为教学对照，不是主线。

低置信度不直接丢。安全场景里，“没看见”比“多调用一次 LLM”更贵。`< 0.50` 会明确标成 `low_confidence` 并进入复核，这个行为有测试覆盖。

事件时间用采集时间，不用发出时间。发出时间会被队列、重试、网络拉长，不能代表“事情是什么时候发生的”。`Event.ts` 是采集时间，`SampleInfo` 里另存 capture、infer、emit、publish 各阶段的时间戳。

SQLite 是事实源，向量库是可重建的索引。时间过滤、幂等、审计都靠 SQLite；向量检索失败时还能回退文本查询。默认向量实现是进程内的，重启后从 SQLite 重建。

Modbus 输出要先过联锁。模型输出不是安全信号。视觉结果先进入 `InterlockController`，连续 N 帧确认、保持、stale/watchdog 都通过后才写寄存器；异常时直接 fail-safe。

## 实测

带宽：1000 帧合成源，原始数据在 [`docs/measurements/metrics-1000.json`](docs/measurements/metrics-1000.json)。

| 指标 | 数值 |
|---|---:|
| 帧数 / 实际 FPS | 1000 / 15.02 |
| JSON 总字节 | 563,944 B（约 564 B/条） |
| 原始 RGB 基线 | 230,400,000 B |
| 相对原始 RGB 降幅 | 99.7552% |
| 相对 15 KB/帧 MJPEG 降幅 | 96.2404% |
| accept / review | 0 / 1000 |

合成源对 ImageNet 模型是分布外输入，置信度只有 0.1%，所以 1000 帧全部进了 review。这个结果说明云端兜底路径能被压满，不代表真实画面的 accept 比例。带宽只统计应用层 payload，不含 TCP/TLS/MQTT 固定头。

v0.3 时间链：200 帧，NTP，+50 ms 偏移，80 ms 截止期。

| 指标 | 数值 |
|---|---:|
| capture → publish p50 / p95 / p99 | 8.825 / 12.183 / 21.750 ms |
| 推理 p50 / p95 | 8.46 / 11.90 ms |
| 截止期错失 / 丢帧 | 0 / 200，0 |

`stage_ms.read` 是等下一帧的采集等待（15 FPS 限速），不计入 capture→publish。早期实现把限速 sleep 算进延迟，p50 会虚高到 66 ms，这个口径后来改掉了。

v0.4 分条件验收：17 类 × 5 条件，40 张标定 / 45 张测试。

| 方案 | coverage | selective accuracy | false accept | ECE |
|---|---:|---:|---:|---:|
| 原始置信度，全局 0.90 | 28.9% | 100% | 0% | 0.267 |
| 温度标定，全局 0.90 | 0% | n/a | 0% | 0.291 |
| 温度标定 + 分条件策略 | 35.6% | 93.8% | 2.2% | 0.291 |

每条件只有 8 张标定图、9 张测试图。这组数字是方法验证，不是出厂参数。测试集 ECE 比标定集高，也说明类别或条件偏移下，标定不是万能药。

v0.5 平面测量：合成针孔相机，15 个棋盘格视图，12 次重复测距。

| 指标 | 数值 |
|---|---:|
| 标定 RMS 重投影 | 0.1384 px |
| 拟合 fx / fy | 906.63 / 907.02 px（真值 900） |
| 100 mm 真值测得 | 100.0899 mm |
| 12 次误差均值 / RMS | 0.0175 / 0.0696 mm |
| 单次标准不确定度 | 0.6096 mm |
| 扩展不确定度 U(k=2) | 1.2369 mm |
| Gauge GRR / 1 mm 公差 | 0.0230 mm / 13.79% |

这是几何链路的合成验证，不是真实相机精度。真实项目要用卡尺/量块做对照，并重新标定。

## 测试

```bash
pytest -q
ruff check src tests scripts
python -m compileall -q src tests
```

当前 72 个测试，覆盖门限边界、契约、预处理、管线、云端降级、API 幂等、Edge-Mem 时间窗/RRF/Token 截断、时钟偏移、ECE/Brier、conformal、SPC、Modbus 往返、联锁、事件时间、激励台、相机标定、平面测量、不确定度和 Gauge R&R。GitHub Actions 在 Python 3.10 和 3.12 上跑 `ruff check` 和 `pytest -q`。

## 限制

模型和数据这块，MobileNetV1 是通用 ImageNet 模型，不是工业缺陷或安全场景专用模型；这组数字用来验证方法，不是识别精度。v0.4 每条件只有 8 张标定、9 张测试，门限方差很大，换数据要重新标定。

运行时这块，`/api/v1/events` 的即时视图还在进程内存里；Edge-Mem 已经落 SQLite，但默认向量索引是进程内的，重启后重建。单消费者串行调用 LLM，换来背压和简单限流，高并发要 worker pool 并按设备保序。

检索和时钟这块，哈希 embedding 不是语义模型，真实语义检索需要替换 `Embedder`。时间解析只覆盖项目里会遇到的中文日期、相对时间和时段表达，不是通用 NLP；识别不了时回退最近 24 小时并标记 `fallback`。采样时钟默认是系统时钟包装，`ntp/ptp/gps` 只是显式标记，不含真实授时驱动。

控制和安全这块，Modbus TCP 是纯标准库模拟从站，不是安全 PLC；联锁逻辑可以验证，但不能替代硬件安全回路。SPC 是诊断信号，不是自动停线依据。设备鉴权还没做，MQTT 用户名密码/TLS 只留了配置位，`POST /api/v1/events` 是本地测试钩子。

测量这块，v0.5 只支持与棋盘格共面的目标；不确定度里的靶面平整度是假设值；合成相机验证的是几何和传播，不是真实镜头精度。真实精度必须用卡尺/量块/三坐标对照，不能把合成误差 0.07 mm 当成出厂指标。

最后，Python 是参考实现。迁到 STM32/ESP32 要替换采集、推理和传输适配器，门限和协议逻辑可以复用，但不是原样复制就能跑。

## 文档

- [协议与 topic](docs/protocol.md)
- [架构、线程模型、故障模式](docs/architecture.md)
- [为什么选 MQTT 而不是 Socket](docs/adr/0001-mqtt-vs-socket.md)
- [Edge-Mem：时间衰减与混合检索](docs/edge-mem.md)
- [仪器化：时间链、标定、SPC、Modbus、激励台](docs/instrumentation.md)
- [平面视觉测量：像素到毫米、不确定度、Gauge R&R](docs/metrology.md)
- [v0.4 分条件验收数据](docs/measurements/v0.4/README.md)
- [v0.5 平面测量验证](docs/measurements/v0.5/README.md)
- [文档索引](docs/README.md)

## License

MIT，见 [LICENSE](LICENSE)。
