# Edge-Cloud Vision System

**端云协同 + 双层过滤的轻量化视觉识别系统**

一个可以用普通 PC 完整跑通、但按真实嵌入式物联网约束设计的开源原型。

> 端侧先做一次"便宜"的判断，只把结构化 JSON 送上云；只有端侧拿不准的样本，才触发边缘大模型兜底。全链路不传输一帧原始图像。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-25%20passed-brightgreen.svg)](tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 项目定位

工业现场的矛盾很具体：

- MCU / 低端 SoC 算力有限，跑不动大模型；
- 摄像头原始视频流持续上行，带宽和流量成本高；
- 云端多模态大模型单次调用有延迟和费用，不可能对每一帧都调用；
- 小模型在模糊、遮挡、未知物体上会给出中等置信度，直接丢弃可能漏掉安全风险。

这个项目用一条完整的端到端链路回答三个问题：

1. **端侧能先过滤掉多少？** 低分辨率采集 + INT8 量化模型，高置信度结果直接生成事件。
2. **过滤不掉的怎么办？** 中等和低置信度样本进入复核，由边缘侧 LLM 做语义判断。
3. **凭什么说省带宽？** 只传结构化 JSON，并用真实帧数同时核算原始 RGB 和 MJPEG 两个基线。

### 适合谁

- 想了解边缘推理、MQTT 可靠通信、LLM 兜底如何拼成一个完整系统的人；
- 想找一个可运行、可测试、可拆解的端云协同最小实现的人；
- 准备把 Python 参考实现迁移到 STM32 / ESP32 的嵌入式开发者。

### 不是什么

- 不是工业级缺陷检测模型。仓库里的 MobileNetV1 是通用 ImageNet 模型，演示的是**架构**，不是识别精度。
- 不是完整产品。没有 Web 前端、没有设备鉴权、没有生产级时序数据库。
- 不是"把视频传给大模型"的方案。原始图像在端侧就被丢弃，只保留结构化字段。

---

## 核心设计

### 两级过滤

```text
置信度 >= 0.90          → accept，直接记录
0.50 <= 置信度 < 0.90   → review，理由 ambiguous
置信度 < 0.50           → review，理由 low_confidence
```

低置信度**不静默丢弃**。在安全场景里，"没看见"比"多调用一次 LLM"更贵，因此 `< 0.50` 也会进入复核，并带有明确的 `reason`。

### 只传结构化事件

端侧发送的是类似下面这样的一条 JSON，通常不到 1 KB：

```json
{
  "schema_version": "1.0",
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
  }
}
```

事件 schema 使用 `extra="forbid"`。任何未声明字段，尤其是 `image` / `frame_b64`，都会在运行时被拒绝。这不是注释里的约定，而是可执行的约束。

### 失败可解释

LLM 未配置、超时、返回非法 JSON 时，不会让主流程崩溃，也不会假装识别成功：

```text
risk_level = unknown
action     = request_human_review
source     = rule_fallback
```

降级结果会写入事件记录，并通过 `/healthz`、`/metrics` 暴露出来。

---

## 系统架构

```text
┌─────────────────────────────── 端侧：模拟 MCU ───────────────────────────────┐
│  OpenCV 采集 320×240  →  center-crop/resize 224×224  →  MobileNetV1 INT8     │
│                                                                              │
│  置信度门限： >= 0.90 accept │ 0.50–0.90 review(ambiguous) │ < 0.50 review    │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │  只发 JSON，无图像
┌───────────────────────────────────────▼──────────────────────────────────────┐
│ 通信层  MQTT 3.1.1 / QoS 1                                                   │
│  ecv/v1/<device>/event   事件，至少一次，云端按 event_id 去重                 │
│  ecv/v1/<device>/status  在线状态 + 心跳 + LWT 离线遗嘱，retained             │
│  断线重连（1–30 s 退避）+ 有界离线缓冲                                        │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼──────────────────────────────────────┐
│ 边缘网关  FastAPI                                                            │
│  订阅 → Pydantic 校验 → 内存事件库 → 仅 review 事件调用 LLM                   │
│  LLM 失败 → rule_fallback：unknown + request_human_review                    │
│  REST：/healthz  /api/v1/events  /metrics                                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 三条边界

- **端侧与云端互不 import。** `edge` 不依赖 `cloud`，`cloud` 也不依赖 `edge`；两边只共享 `contracts.py` 和 `transport`。
- **管线依赖接口，不依赖库。** `EdgeRunner` 依赖 `FrameSource`、`Classifier`、`EventPublisher` 三个 Protocol，不直接依赖 OpenCV、TFLite 或 paho。
- **协议先于实现。** 事件字段、topic、QoS、去重规则都由 `contracts/` 和 `docs/protocol.md` 定义，C++ / ESP32 侧可以按同一份契约实现。

### 技术栈

| 层 | 技术 |
|---|---|
| 端侧推理 | Python 3.10+、OpenCV、ai-edge-litert / TFLite Runtime |
| 通信 | MQTT 3.1.1、paho-mqtt、QoS 1、LWT |
| 边缘网关 | FastAPI、Pydantic、httpx、OpenAI 兼容接口 |
| 测试与 CI | pytest、ruff、GitHub Actions（Python 3.10 / 3.12） |

---

## 目录结构

```text
edge-cloud-vision/
├─ src/ecvision/
│  ├─ contracts.py              # 跨语言事件契约，对应 contracts/*.schema.json
│  ├─ config.py                 # MQTT 环境变量配置
│  ├─ edge/                     # 端侧：采集 / 预处理 / 推理 / 门限 / 指标
│  │  ├─ sources.py             #   FrameSource：摄像头、视频文件、确定性合成源
│  │  ├─ preprocess.py          #   center-crop + BGR→RGB + resize
│  │  ├─ classifier.py          #   TFLite INT8 推理，含 LiteRT / tflite-runtime 回退
│  │  ├─ gate.py                #   纯函数门限，单元测试覆盖边界
│  │  ├─ metrics.py             #   带宽/帧率指标，含两个基线
│  │  ├─ pipeline.py            #   capture → infer → gate → publish
│  │  └─ __main__.py            #   ecv-edge CLI
│  ├─ transport/                # 通信层
│  │  ├─ base.py                #   EventPublisher Protocol + JSON 编码 + DryRun
│  │  ├─ topics.py              #   topic 约定与解析
│  │  └─ mqtt.py                #   paho 发布端：LWT/心跳/重连/离线缓冲
│  └─ cloud/                    # 边缘网关
│     ├─ subscriber.py          #   MQTT 线程 → asyncio 队列
│     ├─ decision.py            #   LLM 调用 + 严格解析 + 保守降级
│     ├─ prompts.py             #   只喂结构化字段的 Prompt
│     ├─ store.py               #   有界内存事件库 + event_id 去重
│     ├─ settings.py            #   环境变量
│     ├─ main.py                #   FastAPI 应用与 REST/metrics
│     └─ __main__.py            #   uvicorn 启动入口
├─ examples/minimal_loop.py     # 一屏版端侧骨架：采集→推理→门限→JSON
├─ .github/workflows/ci.yml     # GitHub Actions：ruff + pytest
├─ tests/                       # 25 个单元/接口测试
├─ scripts/download_model.py    # 下载并校验 MobileNetV1 INT8 + ImageNet labels
├─ contracts/                   # 语言无关 JSON Schema
├─ deploy/mosquitto/            # 本地 broker 配置
├─ docs/                        # 协议、架构说明、ADR
├─ models/                      # 下载的 .tflite / labels.txt，git 忽略
├─ docker-compose.yml           # 一条命令起 Mosquitto
├─ pyproject.toml
└─ .env.example
```

---

## 快速开始

### 环境准备

要求 Python 3.10+。Windows：

```powershell
cd edge-cloud-vision
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[litert,dev]"
```

Linux / macOS：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install tflite-runtime        # 或 ai-edge-litert
```

> **Windows 说明**：PyPI 没有 `tflite-runtime` 的 Windows wheel，因此用 `ai-edge-litert`。代码会依次尝试 `ai_edge_litert.interpreter` → `tflite_runtime.interpreter` → TensorFlow。

### 下载模型

```bash
python scripts/download_model.py
```

脚本会校验压缩包 SHA-256，解出 `models/mobilenet_v1_1.0_224_quant.tflite`，并生成 **1001 行** labels。第 0 类补 `background`，匹配该模型的输出维度。模型约 35 MB，不提交到仓库。

### 最小闭环

不接摄像头、不接 broker，先确认端侧推理和门限能跑：

```bash
python -m ecvision.edge --source synthetic --max-frames 10 --dry-run
```

如果只想看端侧最小代码，见 [`examples/minimal_loop.py`](examples/minimal_loop.py)。约 50 行，采集 → 推理 → 门限 → JSON，不依赖 MQTT 和指标模块：

```bash
python examples/minimal_loop.py
```

输出是一行一条紧凑 JSON，例如：

```json
{"schema_version":"1.0","event_id":"...","device_id":"edge-cam-01","seq":0,"ts":"...","source":{"width":320,"height":240,"fps":15.0},"inference":{"model":"mobilenet_v1_1.0_224_quant","label":"...","label_id":...,"confidence":0.93,"latency_ms":18.4,"top_k":[...]},"gate":{"decision":"accept","reason":"high_confidence","accept_threshold":0.9,"review_threshold":0.5}}
```

摄像头：

```bash
python -m ecvision.edge --source 0 --preview
```

视频文件：

```bash
python -m ecvision.edge --source samples/warehouse.mp4 --loop
```

### 启动本地 MQTT

```bash
docker compose up -d mosquitto
```

如果没有 Docker，可安装 Mosquitto，并使用 [`deploy/mosquitto/mosquitto.conf`](deploy/mosquitto/mosquitto.conf)。只想看端侧结果时，可以继续用 `--dry-run` 跳过 broker。

### 启动边缘网关

```bash
cp .env.example .env       # 填 ECV_LLM_API_KEY；不填也能跑，走 rule_fallback
python -m ecvision.cloud
# http://127.0.0.1:8000/docs
```

> LLM 客户端显式设置 `trust_env=False`，不读取系统的 `HTTP_PROXY/NO_PROXY`。原因是 Windows 上常见的 `NO_PROXY=...,[::1]` 会让 httpx 0.28 在构造客户端时抛 `InvalidURL: Invalid port ':1]'`。需要代理时设置 `ECV_LLM_PROXY=http://127.0.0.1:7890`。

### 端到端联调

终端 1：

```bash
python -m ecvision.cloud
```

终端 2：

```bash
python -m ecvision.edge --source synthetic --max-frames 60 --metrics-out data/metrics.json
```

终端 3：

```bash
curl http://127.0.0.1:8000/healthz
curl "http://127.0.0.1:8000/api/v1/events?limit=5"
cat data/metrics.json
```

---

## 协议与数据契约

完整定义见 [`docs/protocol.md`](docs/protocol.md) 与 [`contracts/event.schema.json`](contracts/event.schema.json)。组件依赖、线程模型和故障模式见 [`docs/architecture.md`](docs/architecture.md)。文档阅读顺序见 [`docs/README.md`](docs/README.md)。

| 方向 | Topic | QoS | Retain | 说明 |
|---|---|---|---|---|
| 端 → 云 | `ecv/v1/{device_id}/event` | 1 | 否 | 单条结构化事件 |
| 端 → 云 | `ecv/v1/{device_id}/status` | 1 | 是 | `online` / `offline` + 心跳 |
| 云 → 端 | `ecv/v1/{device_id}/command` | 1 | 否 | 预留，后续可下发门限或抓图指令 |

几个约束：

- `event_id` 用于云端幂等去重；`seq` 用于发现断线期间的空洞。
- `ts` 必须带时区，Naive datetime 直接拒绝。
- 单条消息上限 64 KB，事件实测通常小于 1 KB。
- MQTT 只保证至少一次，因此云端按 `event_id` 去重，重复消息不会覆盖已有决策。
- `extra="forbid"` 拒绝任何未声明字段，尤其是 `image` / `frame_b64`。

---

## 可靠性设计

| 故障 | 行为 | 可观测性 |
|---|---|---|
| LLM 未配置 API Key | 直接 `rule_fallback`，不发起网络请求 | `/healthz.llm_enabled=false`，`source=rule_fallback` |
| LLM 超时 / 5xx / 网络错误 | 捕获后保守降级，主流程不中断 | `last_error` + `/metrics` 计数 |
| LLM 返回非 JSON / 非法 risk | 解析失败 → 保守降级 | `ecv_llm_fallbacks_total` |
| 网关不支持 `response_format` | 收到 400 后去掉 JSON mode 重试一次 | 日志 `gateway rejected JSON mode` |
| MQTT 断开 | 有界缓冲，默认 500 条；paho 1–30 s 退避重连 | `stats.buffered` / `dropped` |
| 端侧进程崩溃 | Broker 发布 retained `offline` 遗嘱 | `status` topic |
| 云端队列满 | 丢弃并计数，不阻塞 MQTT 回调 | `ecv_events_dropped_total` |

**为什么低置信度不丢弃？** 安全场景里，"没看见"比"多调用一次 LLM"更贵。`< 0.50` 被显式标记为 `low_confidence` 并进入人工/LLM 复核，这个策略有单元测试覆盖。

---

## 实测数据

1000 帧合成源的结果，原始数据见 [`docs/measurements/metrics-1000.json`](docs/measurements/metrics-1000.json)：

| 指标 | 数值 |
|---|---|
| 帧数 / 实际 FPS | 1000 / 15.02 |
| JSON 总字节 | 563,944 B，约 564 B/条 |
| 原始 RGB 基线 | 230,400,000 B |
| 相对原始 RGB 降幅 | **99.7552%** |
| 相对 15 KB/帧 MJPEG 参考降幅 | **96.2404%** |
| accept / review | 0 / 1000 |

合成源对 ImageNet 模型是分布外输入，置信度只有 0.1%，所以 1000 帧全部进入 `review`。这恰好把云端兜底路径压满，但不代表真实场景的 accept 比例。

要统计真实画面的 accept 比例，可以这样跑：

```bash
python -m ecvision.edge --source samples/warehouse.mp4 --max-frames 1000 \
  --metrics-out data/metrics.json
```

取 `metrics.json` 里的 `json_payload_bytes` 与 `raw_rgb_equivalent_bytes` / `jpeg_15kb_reference_bytes`，同时报告两个降幅。带宽结论只统计应用层 payload，不含 TCP/TLS/MQTT 固定头。

---

## 测试

```bash
pytest -q
python -m compileall -q src tests
```

当前覆盖：

- 门限边界：`1.0 / 0.90 → accept`，`0.899 / 0.50 → ambiguous`，`0.499 / 0.0 → low_confidence`；
- 契约：紧凑 JSON < 2 KB、禁止图像字段、时区校验、round-trip；
- 预处理：中心裁剪保持比例、BGR→RGB 通道正确；
- 管线：假采集器 + 假分类器 → 事件字段、指标；
- 云端：无 Key 不联网、保守降级、代码围栏 JSON 解析、非法 risk 拒绝；
- API：注入 review 事件 → 保守决策；重复 `event_id` 幂等且不重复调用 LLM；携带 `image_b64` 返回 422；
- topic：发布/订阅解析、错误层级拒绝。

GitHub Actions 在 Python 3.10 和 3.12 上执行 `ruff check` 与 `pytest -q`。

---

## 已知限制

- 当前模型是通用 ImageNet MobileNetV1，不是工业缺陷/安全场景专用模型；演示的是架构，不是识别精度。
- 事件库是进程内有界存储，重启即丢；生产应换 SQLite/ClickHouse/TSDB。
- 单消费者串行调用 LLM，换取背压和简单的限流；高并发需要 worker pool + 按设备保序。
- 未实现设备鉴权。MQTT 用户名密码/TLS 只留了配置位，`POST /api/v1/events` 是本地测试钩子。
- `metrics` 只统计应用层 payload，不含 TCP/TLS/MQTT 固定头；带宽结论已按此口径标注。
- Python 代码是参考实现。迁移到 STM32 / ESP32 时，需要替换采集、推理和传输适配器；门限与协议逻辑可以复用，但不是"原样复制即可运行"。

---

## License

MIT，见 [LICENSE](LICENSE)。
