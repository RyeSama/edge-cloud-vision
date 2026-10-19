# Edge-Cloud Vision System

**端云协同 + 双层过滤的轻量化视觉识别系统** — 一个可以用普通 PC 完整跑通、但按真实嵌入式物联网约束设计的开源原型。

> 端侧先做一次"便宜"的判断，只把结构化 JSON 送上云；只有端侧拿不准的样本，才触发边缘大模型兜底。
> 全链路不传输一帧原始图像。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-25%20passed-brightgreen.svg)](tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 1. 它解决什么问题

工业现场的矛盾很具体：

- MCU / 低端 SoC 算力有限，跑不动大模型；
- 摄像头原始视频流（哪怕是 320×240 的 MJPEG）持续上行，带宽和流量成本高；
- 云端多模态大模型单次调用有延迟和费用，不可能对每一帧都调用；
- 小模型在模糊、遮挡、未知物体上会给出中等置信度，直接丢弃可能漏掉安全风险。

本项目把问题拆成**两级过滤**：

1. **端侧首层过滤（模块 A）**：低分辨率采集 + INT8 量化小模型。高置信度结果直接生成 JSON 事件；中等置信度标记 `review`；低置信度同样进入 `review`，**不静默丢弃**。
2. **边缘侧兜底（模块 C）**：只对 `review` 事件调用 LLM 做语义决策（风险等级 / 处置建议）。LLM 超时、未配置或返回非法 JSON 时，降级为确定性的 `unknown + request_human_review`，而不是假装成功。

模块 B 是两者之间唯一的通道：MQTT + 严格 JSON 协议，带心跳、遗嘱、断线重连、离线缓冲和幂等去重。

### 关键数字怎么来

"降低 99% 带宽"不能靠感觉。仓库里的 `RunMetrics` 同时记录：

- 本系统实际发送的 JSON 字节数；
- 等帧数原始 RGB 的字节数；
- 以 15 KB/帧为参考的 MJPEG 字节数（**标注为参考值，不是实测**）。

`reduction_vs_raw_rgb` 和 `reduction_vs_jpeg_15kb` 都由真实帧数计算。可以这样描述：

> "在 320×240、15 FPS 的设定下，每帧 RGB 是 230 KB，而一条事件 JSON 约 600–800 B；本仓库 1000 帧实测：JSON 共 563,944 B，原始 RGB 共 230,400,000 B，应用层载荷下降 99.76%；按 15 KB/帧 MJPEG 参考值算是 96.24%。具体数字以 [`docs/measurements/metrics-1000.json`](docs/measurements/metrics-1000.json) 为准，TCP/TLS/MQTT 固定头开销没有算进去。"

---

## 2. 系统架构

```text
┌─────────────────────────────── 模块 A：端侧（模拟 MCU） ───────────────────────────────┐
│  OpenCV 采集 320×240  →  center-crop/resize 224×224  →  MobileNetV1 INT8 (TFLite)     │
│                                                                                      │
│  置信度门限： >= 0.90 accept │ 0.50–0.90 review(ambiguous) │ < 0.50 review(low_conf) │
└───────────────────────────────────────┬──────────────────────────────────────────────┘
                                        │  只发 JSON（无图像）
┌───────────────────────────────────────▼──────────────────────────────────────────────┐
│ 模块 B：通信中间件  MQTT 3.1.1 / QoS 1                                               │
│  ecv/v1/<device>/event  事件（至少一次，云端按 event_id 去重）                         │
│  ecv/v1/<device>/status 在线状态 + 心跳 + LWT 离线遗嘱（retained）                     │
│  断线重连（1–30 s 退避）+ 有界离线缓冲                                                │
└───────────────────────────────────────┬──────────────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼──────────────────────────────────────────────┐
│ 模块 C：边缘网关  FastAPI                                                            │
│  订阅 → Pydantic 校验 → 内存事件库 → 仅 review 事件调用 LLM（OpenAI 兼容接口）          │
│  LLM 失败 → rule_fallback：risk=unknown, action=request_human_review                 │
│  REST：/healthz  /api/v1/events  /metrics                                            │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 为什么这样分层

- **`edge` 不 import `cloud`，`cloud` 也不 import `edge`**；两边只共享 `contracts.py` 和 `transport`。这样才有资格说"模块解耦"。
- `EdgeRunner` 依赖 `FrameSource` / `Classifier` / `EventPublisher` 三个 Protocol，不依赖 OpenCV、TFLite、paho 的具体实现。移植到 STM32/ESP32 时，替换的是最外层适配器，门限与协议逻辑可以原样翻译成 C。
- 事件 schema 用 `extra="forbid"` 强制"禁止携带图像字段"。这不是注释里的约定，而是运行时会拒绝的约束。

---

## 3. 目录结构

```text
edge-cloud-vision/
├─ src/ecvision/
│  ├─ contracts.py              # 跨语言事件契约（Pydantic，对应 contracts/*.schema.json）
│  ├─ config.py                 # MQTT 环境变量配置
│  ├─ edge/                     # 模块 A：采集 / 预处理 / 推理 / 门限 / 指标
│  │  ├─ sources.py             #   FrameSource：摄像头、视频文件、确定性合成源
│  │  ├─ preprocess.py          #   center-crop + BGR→RGB + resize
│  │  ├─ classifier.py          #   TFLite INT8 推理（LiteRT / tflite-runtime / TF 回退）
│  │  ├─ gate.py                #   纯函数门限，单元测试覆盖边界
│  │  ├─ metrics.py             #   带宽/帧率指标（含基线，避免自欺）
│  │  ├─ pipeline.py            #   capture → infer → gate → publish
│  │  └─ __main__.py            #   ecv-edge CLI
│  ├─ transport/                # 模块 B：传输边界
│  │  ├─ base.py                #   EventPublisher Protocol + JSON 编码 + DryRun
│  │  ├─ topics.py              #   topic 约定与解析
│  │  └─ mqtt.py                #   paho 发布端：LWT/心跳/重连/离线缓冲
│  └─ cloud/                    # 模块 C：FastAPI 网关
│     ├─ subscriber.py          #   MQTT 线程 → asyncio 队列（回调不做 I/O）
│     ├─ decision.py            #   LLM 调用 + 严格解析 + 保守降级
│     ├─ prompts.py             #   Prompt Engineering（只喂结构化字段）
│     ├─ store.py               #   有界内存事件库 + event_id 去重
│     ├─ settings.py            #   环境变量
│     ├─ main.py                #   FastAPI 应用与 REST/metrics
│     └─ __main__.py            #   uvicorn 启动入口
├─ examples/minimal_loop.py     # 一屏版模块 A 骨架：采集→推理→门限→JSON
├─ .github/workflows/ci.yml      # GitHub Actions：ruff + pytest（3.10 / 3.12）
├─ tests/                       # 25 个单元/接口测试（门限/契约/预处理/管线/降级/topic/API）
├─ scripts/download_model.py    # 下载并校验 MobileNetV1 INT8 + ImageNet labels
├─ contracts/                   # 语言无关 JSON Schema（供 C++/ESP32 复用）
├─ deploy/mosquitto/            # 本地 broker 配置
├─ docs/                        # 协议、架构说明、ADR（MQTT 选型）
├─ models/                      # 下载的 .tflite / labels.txt（git 忽略）
├─ docker-compose.yml           # 一条命令起 Mosquitto
├─ pyproject.toml
└─ .env.example
```

---

## 4. 快速开始

### 4.1 环境

要求 Python 3.10+。Windows 上推荐：

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

> **Windows 说明**：PyPI 没有 `tflite-runtime` 的 Windows wheel，因此用 `ai-edge-litert`。
> 代码会依次尝试 `ai_edge_litert.interpreter` → `tflite_runtime.interpreter` → TensorFlow。

### 4.2 下载模型（一次性，约 35 MB）

```bash
python scripts/download_model.py
```

脚本会校验压缩包 SHA-256，解出 `models/mobilenet_v1_1.0_224_quant.tflite`，并生成 **1001 行** labels（第 0 类补 `background`，匹配该模型的输出维度）。

### 4.3 不接摄像头、不接 broker，先跑通最小闭环

```bash
python -m ecvision.edge --source synthetic --max-frames 10 --dry-run
```

如果只想看模块 A 的最小代码，见 [`examples/minimal_loop.py`](examples/minimal_loop.py)：
约 50 行，采集 → 推理 → 门限 → JSON，不依赖 MQTT 和指标模块。

```bash
python examples/minimal_loop.py
```

你会看到每帧一行紧凑 JSON，以及类似：

```json
{"schema_version":"1.0","event_id":"...","device_id":"edge-cam-01","seq":0,"ts":"...","source":{"width":320,"height":240,"fps":15.0},"inference":{"model":"mobilenet_v1_1.0_224_quant","label":"...","label_id":...,"confidence":0.93,"latency_ms":18.4,"top_k":[...]},"gate":{"decision":"accept","reason":"high_confidence","accept_threshold":0.9,"review_threshold":0.5}}
```

摄像头：

```bash
python -m ecvision.edge --source 0 --preview        # Windows 默认 DSHOW
```

视频文件：

```bash
python -m ecvision.edge --source samples/warehouse.mp4 --loop
```

### 4.4 启动本地 MQTT broker

```bash
docker compose up -d mosquitto
```

或者按 [`docs/protocol.md`](docs/protocol.md) 安装 Mosquitto，配置见 [`deploy/mosquitto/mosquitto.conf`](deploy/mosquitto/mosquitto.conf)。

### 4.5 启动云端网关

```bash
cp .env.example .env       # 填 ECV_LLM_API_KEY；不填也能跑，走 rule_fallback
python -m ecvision.cloud
# http://127.0.0.1:8000/docs
```

> LLM 客户端显式设置 `trust_env=False`，不读取系统的 `HTTP_PROXY/NO_PROXY`。原因是
> Windows 上常见的 `NO_PROXY=...,[::1]` 会让 httpx 0.28 在构造客户端时抛
> `InvalidURL: Invalid port ':1]'`。需要代理时设置 `ECV_LLM_PROXY=http://127.0.0.1:7890`。

### 4.6 端到端

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

## 5. 协议摘要

完整定义见 [`docs/protocol.md`](docs/protocol.md) 与 [`contracts/event.schema.json`](contracts/event.schema.json)；
组件依赖、线程模型和故障模式见 [`docs/architecture.md`](docs/architecture.md)；
全部文档的阅读顺序见 [`docs/README.md`](docs/README.md)。

| 方向 | Topic | QoS | Retain | 说明 |
|---|---|---|---|---|
| 端 → 云 | `ecv/v1/{device_id}/event` | 1 | 否 | 单条结构化事件 |
| 端 → 云 | `ecv/v1/{device_id}/status` | 1 | 是 | `online` / `offline` + 心跳 |
| 云 → 端 | `ecv/v1/{device_id}/command` | 1 | 否 | 预留（改门限、抓图指令等） |

事件核心字段：

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

设计要点：

- `event_id` 用于云端幂等去重；`seq` 用于发现断线期间的空洞。
- `ts` 必须带时区；Naive datetime 直接拒绝。
- `extra="forbid"`：任何未声明字段（尤其 `image` / `frame_b64`）都会被拒绝。
- 单条消息上限 64 KB；事件实测通常 < 1 KB。
- MQTT 只保证至少一次，因此云端 `EventStore.add` 以 `event_id` 去重，重复消息不会覆盖已有决策。

---

## 6. 降级策略（为什么这样设计）

| 故障 | 行为 | 可观测性 |
|---|---|---|
| LLM 未配置 API Key | 直接 `rule_fallback`，不发起网络请求 | `/healthz.llm_enabled=false`，`source=rule_fallback` |
| LLM 超时 / 5xx / 网络错误 | 捕获后保守降级，主流程不中断 | `last_error` + `/metrics` 计数 |
| LLM 返回非 JSON / 非法 risk | 解析失败 → 保守降级 | `ecv_llm_fallbacks_total` |
| 网关不支持 `response_format` | 收到 400 后去掉 JSON mode 重试一次 | 日志 `gateway rejected JSON mode` |
| MQTT 断开 | 有界缓冲（默认 500 条），paho 1–30 s 退避重连 | `stats.buffered` / `dropped` |
| 端侧进程崩溃 | Broker 发布 retained `offline` 遗嘱 | `status` topic |
| 云端队列满 | 丢弃并计数，不阻塞 MQTT 回调 | `ecv_events_dropped_total` |

**为什么低置信度不丢弃？** 安全场景里，"没看见"比"多调用一次 LLM"更贵。`< 0.50` 被显式标记为 `low_confidence` 并进入人工/LLM 复核，这个策略有单元测试覆盖。

---

## 7. 测试与实验

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

本机实测（1000 帧合成源，[`docs/measurements/metrics-1000.json`](docs/measurements/metrics-1000.json)）：

| 指标 | 数值 |
|---|---|
| 帧数 / 实际 FPS | 1000 / 15.02 |
| JSON 总字节 | 563,944 B（约 564 B/条） |
| 原始 RGB 基线 | 230,400,000 B |
| 相对原始 RGB 降幅 | **99.7552%** |
| 相对 15 KB/帧 MJPEG 参考降幅 | **96.2404%** |
| accept / review | 0 / 1000 |

合成源对 ImageNet 模型是分布外输入，置信度只有 0.1%，所以 1000 帧全部进入 `review`——
这恰好把云端兜底路径压满，但不代表真实场景的 accept 比例。要统计 accept 比例，请用
`--source samples/warehouse.mp4` 或摄像头跑真实画面；门限行为本身由单元测试覆盖。

带宽实验（建议自己跑一遍）：

```bash
python -m ecvision.edge --source samples/warehouse.mp4 --max-frames 1000 \
  --metrics-out data/metrics.json
```

取 `metrics.json` 里的 `json_payload_bytes` 与 `raw_rgb_equivalent_bytes` / `jpeg_15kb_reference_bytes`，报告**两个**降幅，不要让"99%"脱离基线。

---

## 8. 开发路线（7 天）

> 目标不是"功能多"，而是第 7 天能演示：**断网不丢事件、LLM 挂掉不崩、带宽数字可复现、README 能讲清楚**。

### Day 1 — 骨架与契约先行

- 建 `src` 布局、`pyproject.toml`、`.env.example`、`.gitignore`；
- 定义 `Event` / `Decision` Pydantic 契约，`extra="forbid"`；
- 写门限纯函数 + 边界单元测试（先红后绿）；
- **验收**：`pytest` 全绿；`contracts/event.schema.json` 能生成。

### Day 2 — 模块 A 最小闭环

- `OpenCVSource`（摄像头/视频/合成源）+ `center_crop` + `to_rgb_input`；
- `TFLiteClassifier`：读取 input shape/dtype，支持 uint8/int8/float32，INT8 反量化；
- `EdgeRunner` 串起 capture → infer → gate → JSON；
- **验收**：`--source synthetic --dry-run` 连续输出 100 帧，无内存增长；真实模型 top-1 合理。

### Day 3 — 模块 B：协议与可靠性

- MQTT topic 设计、QoS 1、LWT、retained 在线状态、心跳；
- 断线重连 + 有界缓冲；`event_id` 幂等；
- 用 Mosquitto 抓包/订阅验证；
- **验收**：`mosquitto_sub -t 'ecv/v1/+/event' -v` 能看到 JSON；kill 端侧后 status 变 `offline`。

### Day 4 — 模块 C：云端与 LLM 兜底

- FastAPI lifespan 管理 subscriber/queue/consumer；
- SSE？不需要。REST + `/metrics` 足够；
- Prompt 只喂结构化字段；JSON mode + 失败重试一次；
- 降级策略与单元测试；
- **验收**：`POST /api/v1/events` 注入 review 事件，`GET` 能看到 `source=llm` 或 `rule_fallback`。

### Day 5 — 联调与指标

- 端到端跑 1000 帧，生成 `metrics.json`；
- 统计 accept/review 比例、平均推理延迟、JSON 大小分布；
- 画一张"带宽 vs 帧数"图（matplotlib 可选，不要引入运行时依赖）；
- **验收**：README 里的数字全部来自这次实验。

### Day 6 — 故障演练

- 停 broker：端侧继续采集，缓冲增长；恢复后补发；
- 停 LLM / 填错 Key：review 事件全部 `rule_fallback`，进程不崩；
- 发畸形 JSON / 超大消息：云端计数 `invalid`，不影响后续消息；
- 队列打满：验证丢弃计数与日志；
- **验收**：四条演练写进 README 的"故障注入"表。

### Day 7 — 文档与演示

- README：架构图、快速开始、协议、指标、限制；
- `docs/adr/0001-mqtt-vs-socket.md` 记录选型理由；
- 一段 30–60 秒演示 GIF（端侧窗口 + 云端日志）；
- **验收**：新用户在干净机器上按 README 30 分钟内能跑通。

### 第一周不做的事

- 不训练自定义模型（用现成 INT8 MobileNet 先验证架构）；
- 不做 Web 前端（FastAPI `/docs` + JSON 足够）；
- 不做 Kubernetes / 微服务拆分；
- 不追求"支持 10 路视频"——单路跑稳，再谈扩展。

---

## 9. 已知限制

- 当前模型是通用 ImageNet MobileNetV1，不是工业缺陷/安全场景专用模型；演示的是**架构**，不是识别精度。
- 事件库是进程内有界存储，重启即丢；生产应换 SQLite/ClickHouse/TSDB。
- 单消费者串行调用 LLM，换取背压和简单的限流；高并发需 worker pool + 按设备保序。
- 未实现设备鉴权（MQTT 用户名密码/TLS 只留了配置位），`POST /api/v1/events` 是本地测试钩子。
- `metrics` 只统计应用层 payload，不含 TCP/TLS/MQTT 固定头；带宽结论已按此口径标注。

## 10. License

MIT，见 [LICENSE](LICENSE)。
