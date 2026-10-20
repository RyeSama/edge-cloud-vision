# Edge-Cloud Vision System

**端云协同 + 双层过滤的轻量化视觉识别系统**

一个可以用普通 PC 完整跑通、但按真实嵌入式物联网约束设计的开源原型。

> 端侧先做一次"便宜"的判断，只把结构化 JSON 送上云；只有端侧拿不准的样本，才触发边缘大模型兜底。全链路不传输一帧原始图像。
>
> v0.2.0 新增 **Edge-Mem**：把事件按时间衰减分层存储，并提供“时间硬过滤 + 语义向量 + RRF 融合”的混合检索，让网关能回答“昨天下午那个穿红衣服的人是什么时候出现的？”
>
> v0.3.0 新增 **仪器化层**：采样时钟、端到端延迟/抖动/截止期/丢帧、置信度标定与 conformal 集合、Modbus TCP 联锁、确定性激励台、SPC 漂移监控和事件时间 watermark。
>
> v0.4.0 新增 **分条件验收**：温度标定 + 按 false-accept 预算为 clean/blur/occlusion/lowlight/downscale 分别选 accept 门限；在独立测试集上 coverage 28.9% → 35.6%，false accept 0% → 2.2%。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-65%20passed-brightgreen.svg)](tests)
[![Release](https://img.shields.io/badge/release-v0.4.0-blue.svg)](https://github.com/RyeSama/edge-cloud-vision/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 大白话版本

> 下面这段不需要任何前置知识。看完再看后面的术语。

### 一句话

摄像头看到画面，先让端侧小模型“瞄一眼”：有把握就发一条几百字节的文字消息；没把握就把这条消息标成“需要复核”，让云端大模型帮忙判断。**全程不传视频，也不传图片。**

### 为什么不用把视频直接传给大模型

把原始视频一直传给云端，就像每秒钟都把整本相册上传一次：带宽贵、延迟高、云端调用也贵。实际上大部分画面是重复的、正常的；只有少数“看不清、拿不准”的画面，才值得花大模型的钱。

### 系统里有什么

- **端侧**：低分辨率采集 + 轻量模型，先做第一次判断；拿不准就标记，不静默丢掉。
- **网络**：只发一小段 JSON 文字（设备号、识别结果、置信度、时间），带心跳和断线重连；断网时先存着，恢复后补发。
- **云端**：高置信度直接记录；需要复核的才调用大模型；大模型挂了就返回“未知，请人工复核”，不假装成功。
- **记忆**：把事件按时间存起来，能回答“昨天下午那个穿红衣服的人什么时候出现的”。
- **测量**：给每帧打时间戳，统计处理花了多久、有没有超时或丢帧；给模型做“校准”，让 0.9 更接近真实的 90%。
- **控制**：识别结果可以写到 Modbus 寄存器，连续几帧确认才报警；掉线或卡死会自动进入安全状态。

### 四个版本做了什么

- **v0.1**：能跑通“采集 → 识别 → 发消息 → 云端兜底”。
- **v0.2**：能记住过去发生的事，并按时间/语义检索。
- **v0.3**：能证明“测得准、反应快、坏了不慌”——时间链、标定、Modbus 联锁、激励台、SPC。
- **v0.4**：不再用一个 0.90 卡所有场景，而是按清晰/模糊/遮挡/暗光/降采样分别定门限，并报告“放行多少、错了多少”。

### 它不是什么

- 不是一个“识别准确率 99%”的工业模型；仓库里的 MobileNet 是通用 ImageNet 模型，演示的是系统架构。
- 不是把视频传给大模型；原始图像在端侧就丢了。
- 不是生产级安全 PLC；Modbus 联锁是软件模拟，不能替代硬件安全回路。
- README 里的数字来自小样本实验，用来证明方法可复现，不是出厂指标。

**下面的章节开始上术语。**

---

## 项目定位

工业现场的矛盾很具体：

- MCU / 低端 SoC 算力有限，跑不动大模型；
- 摄像头原始视频流持续上行，带宽和流量成本高；
- 云端多模态大模型单次调用有延迟和费用，不可能对每一帧都调用；
- 小模型在模糊、遮挡、未知物体上会给出中等置信度，直接丢弃可能漏掉安全风险。

这个项目用一条完整的端到端链路回答四个问题：

1. **端侧能先过滤掉多少？** 低分辨率采集 + INT8 量化模型，高置信度结果直接生成事件。
2. **过滤不掉的怎么办？** 中等和低置信度样本进入复核，由边缘侧 LLM 做语义判断。
3. **凭什么说省带宽？** 只传结构化 JSON，并用真实帧数同时核算原始 RGB 和 MJPEG 两个基线。
4. **过去发生了什么？** Edge-Mem 把事件按时间衰减分层，用“时间硬过滤 + 语义检索 + RRF”回答自然语言时间问题。

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

事件 schema 使用 `extra="forbid"`。任何未声明字段，尤其是 `image` / `frame_b64`，都会在运行时被拒绝。这不是注释里的约定，而是可执行的约束。

### 失败可解释

LLM 未配置、超时、返回非法 JSON 时，不会让主流程崩溃，也不会假装识别成功：

```text
risk_level = unknown
action     = request_human_review
source     = rule_fallback
```

降级结果会写入事件记录，并通过 `/healthz`、`/metrics` 暴露出来。

### Edge-Mem：时间感知的记忆层

v0.2.0 在云端事件落库之后增加了一层“可检索记忆”：

```text
hot    最近 10 分钟       完整事件明细           SQLite + 向量
warm   10 分钟 – 2 小时    15 分钟聚合摘要        SQLite + 摘要向量
cold   2 小时以上         极简索引，raw 已删除    仅 SQLite 时间过滤
```

查询时先用正则/轻量 NLP 解析时间窗，在 SQLite 做硬过滤，再在候选集内做向量检索，最后用 **RRF（Reciprocal Rank Fusion）** 融合时间排名与语义排名。向量库失败会降级为 SQLite 文本查询并标记 `degraded`；没有 LLM 时返回 `json_timeline` 而不是报错。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"昨天下午穿红衣服的人是什么时候出现的？","limit":10,"format":"markdown"}'
```

细节、公式和配置见 [`docs/edge-mem.md`](docs/edge-mem.md)。

### v0.3：从“识别 demo”到“测量仪器”

v0.3 补的不是更多类别，而是**测量可信度**：

- **时间链**：`SamplingClock` 同时记录单调时钟与墙钟；`Event.ts` 是采集时间，`SampleInfo` 保存 capture → infer → emit 各阶段时间戳；`RunMetrics` 再记录发布完成时刻，输出 p50/p95/p99、抖动、截止期错失、丢帧率，并区分 `clock.synced=false`。
- **置信度标定**：等渗回归 + ECE/Brier；conformal prediction set 大小 > 1 时直接 `review`，不再把 softmax 当概率。
- **控制闭环**：纯标准库 Modbus TCP 从站（coil/holding/input register），联锁状态机支持连续 N 帧确认、5 s 保持、stale/watchdog → fail-safe、reset/silence/capture 命令 ACK。
- **可复现验证**：确定性距离/照度/模糊/遮挡激励矩阵；EWMA + CUSUM + 标签分布 JS 散度的 SPC 监控；事件时间 watermark 与晚到事件标记。

完整说明见 [`docs/instrumentation.md`](docs/instrumentation.md)。

```bash
# 时间链 + 截止期
python -m ecvision.edge --source synthetic --max-frames 200 \
  --clock-source ntp --clock-offset-ms 50 --deadline-ms 80 \
  --metrics-out data/clock.json

# 控制闭环：Modbus TCP 127.0.0.1:15020
python -m ecvision.edge --source synthetic --max-frames 300 \
  --interlock --modbus --modbus-port 15020

# 确定性激励台
python scripts/bench_stimulus.py --out docs/measurements/stimulus.md
```

### v0.4：同一模型，不一定要用同一个门限

v0.3 的标定解决了“softmax 不是概率”，但默认仍然是全局 0.90/0.50。问题是：clean、blur、occlusion、lowlight、downscale 的分数分布根本不一样，用一个门限会同时造成“clean 太保守”和“blur 太激进”。

v0.4 把验收做成可量化的工程权衡：

- **温度标定**：在标定集上拟合 `T=3.22`，降低过度自信；
- **分条件策略**：在 false-accept 预算（默认 5%）下，为每个条件单独选 accept 门限；
- **独立测试**：按类别切分 40 张标定 / 45 张测试，测试类别不参与选门限；
- **报告口径**：同时给 accuracy、coverage、selective accuracy、false accept、ECE，不允许只报一个 accuracy。

45 张测试集上的结果：

| 方案 | coverage | selective accuracy | false accept |
|---|---:|---:|---:|
| 原始置信度，全局 0.90 | 0.289 | 1.000 | 0.000 |
| 温度标定，全局 0.90 | 0.000 | n/a | 0.000 |
| 温度标定 + 分条件策略 | 0.356 | 0.938 | 0.022 |

分条件策略把 coverage 从 28.9% 提到 35.6%，代价是 2.2% false accept。这个数字不大，但它把“用多少误接受换多少覆盖率”变成了可复现的报表，而不是拍脑袋。

完整数据、门限表和复现命令见 [`docs/measurements/v0.4/README.md`](docs/measurements/v0.4/README.md)。

> 注意：每条件只有 8 张标定图、9 张测试图。这是方法验证，不是出厂参数；真实产线必须用现场标注数据重新标定。

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
│  订阅 → Pydantic 校验 → 内存即时视图 → 仅 review 事件调用 LLM           │
│  LLM 失败 → rule_fallback：unknown + request_human_review                    │
│  REST：/healthz  /api/v1/events  /metrics                                    │
└───────────────────────────────────────┬──────────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼──────────────────────────────────────┐
│ Edge-Mem 记忆层                                                              │
│  SQLite：hot 明细 / warm 摘要 / cold 极简索引                                 │
│  时间解析 → 硬过滤 → 时间排名 + 向量排名 → RRF → Token 预算上下文             │
│  向量失败 → SQLite 文本回退（degraded）   LLM 未配置 → JSON 时间轴            │
│  REST：/api/v1/memory/query  /ask  /compress  /stats                         │
└──────────────────────────────────────────────────────────────────────────────┘
┌────────────────────────────── 仪器化 / 控制 v0.3 ─────────────────────────────┐
│  SamplingClock：mono + wall + boot_id + sync 状态                            │
│  SampleInfo：capture → read → infer → emit → publish 阶段时间戳              │
│  CalibrationGate：isotonic + conformal set → accept/review                    │
│  Interlock：连续 N 帧 / 保持 / stale / watchdog → Modbus TCP 寄存器           │
│  Bench + SPC：确定性激励矩阵、EWMA/CUSUM、标签分布漂移                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 四条边界

- **端侧与云端互不 import。** `edge` 不依赖 `cloud`，`cloud` 也不依赖 `edge`；两边只共享 `contracts.py` 和 `transport`。
- **管线依赖接口，不依赖库。** `EdgeRunner` 依赖 `FrameSource`、`Classifier`、`EventPublisher` 三个 Protocol，不直接依赖 OpenCV、TFLite 或 paho。
- **协议先于实现。** 事件字段、topic、QoS、去重规则都由 `contracts/` 和 `docs/protocol.md` 定义，C++ / ESP32 侧可以按同一份契约实现。
- **控制输出不绕过联锁。** 视觉结果先进入 `InterlockController`，再由 `ModbusRegisterSink` 写寄存器；stale/watchdog 直接 fail-safe，不把模型输出当安全信号。

### 技术栈

| 层 | 技术 |
|---|---|
| 端侧推理 | Python 3.10+、OpenCV、ai-edge-litert / TFLite Runtime |
| 通信 | MQTT 3.1.1、paho-mqtt、QoS 1、LWT |
| 边缘网关 | FastAPI、Pydantic、httpx、OpenAI 兼容接口 |
| Edge-Mem | SQLite（WAL）、向量索引 Protocol、RRF、可选 Chroma |
| 仪器化 / 控制 | 单调+墙钟、isotonic/conformal、Modbus TCP、联锁状态机、EWMA/CUSUM |
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
│  ├─ cloud/                    # 边缘网关
│     ├─ subscriber.py          #   MQTT 线程 → asyncio 队列
│     ├─ decision.py            #   LLM 调用 + 严格解析 + 保守降级
│     ├─ prompts.py             #   只喂结构化字段的 Prompt
│     ├─ store.py               #   有界内存事件库 + event_id 去重
│     ├─ settings.py            #   环境变量
│     ├─ main.py                #   FastAPI 应用与 REST/metrics
│     └─ __main__.py            #   uvicorn 启动入口
│  └─ memory/                   # v0.2 Edge-Mem：时间衰减 + 混合检索
│     ├─ models.py              #   严格契约：MemoryItem/TimeWindow/QueryResult
│     ├─ repository.py          #   SQLite：events / summaries / cold_events
│     ├─ vector.py              #   VectorIndex Protocol + 内存/Chroma 适配器
│     ├─ embedder.py            #   Embedder Protocol + 确定性哈希向量
│     ├─ timeparse.py           #   中文日期/相对时间/时段 → 时间窗
│     ├─ compression.py         #   hot → warm 聚合、warm/cold 归档
│     ├─ retriever.py           #   时间+语义召回、RRF、降级
│     ├─ context.py             #   Markdown/JSON 时间轴 + Token 预算
│     ├─ answer.py              #   可选 LLM 回答（OpenAI 兼容）
│     └─ service.py             #   编排：入库、查询、压缩、统计
│  ├─ instrument/               # v0.3/v0.4：采样时钟、标定、SPC、验收策略
│  │  ├─ clock.py               #   mono + wall + sync 状态
│  │  ├─ calibration.py         #   isotonic/temperature、ECE/Brier、conformal set
│  │  ├─ acceptance.py          #   v0.4：分条件 accept 门限 + false-accept 预算
│  │  └─ spc.py                 #   EWMA/CUSUM + 标签分布漂移
│  ├─ control/                  # v0.3：Modbus TCP + 联锁
│  │  ├─ modbus.py              #   纯标准库 Modbus TCP 从站
│  │  └─ interlock.py           #   去抖/保持/stale/watchdog/fail-safe
│  └─ bench/                    # v0.3：确定性激励矩阵
│     └─ stimulus.py            #   距离/照度/模糊/遮挡 + 重复性报告
├─ examples/minimal_loop.py     # 一屏版端侧骨架：采集→推理→门限→JSON
├─ .github/workflows/ci.yml     # GitHub Actions：ruff + pytest
├─ tests/                       # 65 个单元/接口测试
├─ scripts/download_model.py    # 下载并校验 MobileNetV1 INT8 + ImageNet labels
├─ scripts/bench_memory.py      # Edge-Mem 微基准（压缩 + 查询延迟）
├─ scripts/bench_stimulus.py    # v0.3 激励矩阵报告
├─ scripts/fit_calibration.py   # v0.3 标定 / conformal profile
├─ scripts/fit_policy.py        # v0.4 分条件 accept 门限
├─ scripts/split_manifest.py    # v0.4 按类别切分标定/测试集
├─ scripts/eval_dataset.py      # 带 ground truth 的数据集验收
├─ contracts/                   # 语言无关 JSON Schema
├─ deploy/mosquitto/            # 本地 broker 配置
├─ docs/                        # 协议、架构、ADR、Edge-Mem、仪器化
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
{"schema_version":"1.1","event_id":"...","device_id":"edge-cam-01","seq":0,"ts":"...","source":{"width":320,"height":240,"fps":15.0},"inference":{"model":"mobilenet_v1_1.0_224_quant","label":"...","label_id":...,"confidence":0.93,"latency_ms":18.4,"top_k":[...]},"gate":{"decision":"accept","reason":"high_confidence","accept_threshold":0.9,"review_threshold":0.5}}
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

默认使用内存向量索引，不需要额外依赖；想用 Chroma 持久化：

```bash
pip install -e ".[memory]"
# .env 里设置 ECV_MEMORY_VECTOR_BACKEND=chroma
```

### 试用 Edge-Mem

网关启动后，端侧事件会自动进入 SQLite。先注入一条事件，再查记忆：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/events \
  -H 'Content-Type: application/json' \
  -d @docs/examples/review-event.json

curl http://127.0.0.1:8000/api/v1/memory/stats

curl -X POST http://127.0.0.1:8000/api/v1/memory/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"过去 1 小时有哪些人？","limit":5,"format":"markdown"}'
```

`/api/v1/memory/ask` 在未配置 LLM 时返回 `mode="json_timeline"`；配置 LLM 后返回 `mode="llm"`。完整说明见 [`docs/edge-mem.md`](docs/edge-mem.md)。

### 试用 v0.3 仪器化

时间链与截止期：

```bash
python -m ecvision.edge --source synthetic --max-frames 200 \
  --clock-source ntp --clock-offset-ms 50 --deadline-ms 80 \
  --metrics-out data/clock.json
```

控制闭环（Modbus TCP 从站）：

```bash
python -m ecvision.edge --source synthetic --max-frames 300 \
  --interlock --modbus --modbus-port 15020 \
  --assert-after 3 --clear-after-s 5 --stale-after-s 2
```

标定（需要自备 JSONL，格式见 [`docs/instrumentation.md`](docs/instrumentation.md)）：

```bash
python scripts/fit_calibration.py \
  --input data/calibration.jsonl \
  --out data/calibration.json \
  --conformal-out data/conformal.json
```

确定性激励台：

```bash
python scripts/bench_stimulus.py --out docs/measurements/stimulus.md
```

完整字段、寄存器映射和验收清单见 [`docs/instrumentation.md`](docs/instrumentation.md)。

### 试用 v0.4 分条件验收

用带 `label` 和 `degradation` 列的 manifest，先切分再标定/选门限：

```bash
python scripts/split_manifest.py --input data/manifest.csv \
  --calibration-out data/manifest-cal.csv --test-out data/manifest-test.csv \
  --calibration-fraction 0.5 --seed 7

python scripts/eval_dataset.py --manifest data/manifest-cal.csv --out-dir data/cal-raw
python scripts/fit_calibration.py --input data/cal-raw/predictions.csv \
  --method temperature --out data/calibration-v04.json

python scripts/eval_dataset.py --manifest data/manifest-cal.csv --out-dir data/cal-temp \
  --calibration-profile data/calibration-v04.json
python scripts/fit_policy.py --input data/cal-temp/predictions.csv \
  --false-accept-budget 0.05 --min-samples 20 --out data/acceptance-policy-v04.json

python scripts/eval_dataset.py --manifest data/manifest-test.csv --out-dir data/test-policy \
  --calibration-profile data/calibration-v04.json \
  --policy data/acceptance-policy-v04.json
```

`--min-samples` 是每个条件的最少标定样本数；少于它时该条件回退到全局默认门限，不硬拟合。完整示例和 85 张图的结果见 [`docs/measurements/v0.4/README.md`](docs/measurements/v0.4/README.md)。

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
| 向量库检索失败 | 回退 SQLite 文本/标签过滤，`degraded=true` | `/api/v1/memory/*.warnings`、`last_vector_error` |
| Chroma 未安装/初始化失败 | 使用进程内向量索引，记录 warning | `/healthz.memory_warnings` |
| Edge-Mem SQLite 初始化失败 | v0.1 事件主链路继续，memory API 返回 503 | `/healthz.memory_error` |
| LLM 未配置（记忆问答） | 返回 `json_timeline`，保留检索时间轴 | `/api/v1/memory/ask.mode` |

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

Edge-Mem 的微基准（[`docs/measurements/edge-mem.json`](docs/measurements/edge-mem.json)，1000 条事件、3 小时、5 类标签、2 台设备）：
`ingest` 2.62 s，`compress` 1.14 s；压缩后 hot 56 / warm 64 / cold 333，向量 120 条；查询 p50 2.94 ms、p95 5.36 ms，候选 87 条，返回 10 条，上下文 509 tokens，无截断。
这是单机微基准，不是生产容量结论；它只说明默认离线实现能在毫秒级完成一次“时间过滤 + 向量召回 + RRF + 上下文组装”。

v0.3 时间链实测（[`docs/measurements/clock-v03.json`](docs/measurements/clock-v03.json)，200 帧、NTP、+50 ms 偏移、80 ms 截止期）：

| 指标 | 数值 |
|---|---|
| capture → publish p50 / p95 / p99 | 8.825 / 12.183 / 21.750 ms |
| 推理 p50 / p95 | 8.46 / 11.90 ms |
| 帧间隔 p50 / 抖动 p95 | 66.75 / 11.50 ms |
| 截止期错失 / 丢帧 | 0 / 200，0 |
| 时钟同步 | synced 200 / unsynced 0 |

`stage_ms.read` 是等下一帧的采集等待（15 FPS 限速），不计入 capture→publish；这个口径修正本身也是 v0.3 的一部分：早期实现把限速 sleep 算进延迟，p50 会虚高到 66 ms。

v0.3 激励台（[`docs/measurements/stimulus.md`](docs/measurements/stimulus.md)，8 场景 × 3 次，真实 MobileNetV1 INT8）：24 个样本全部 `review`，置信度 0.0011–0.0018，推理 p50 8.11–11.76 ms；合成目标无 ground truth，`accuracy` 为 `n/a`。它证明确定性和时序稳定性，不证明识别精度。

v0.4 分条件验收（[`docs/measurements/v0.4/README.md`](docs/measurements/v0.4/README.md)，17 类 × 5 条件，40 张标定 / 45 张测试）：

| 方案 | coverage | selective accuracy | false accept | ECE |
|---|---:|---:|---:|---:|
| 原始置信度，全局 0.90 | 0.289 | 1.000 | 0.000 | 0.267 |
| 温度标定，全局 0.90 | 0.000 | n/a | 0.000 | 0.291 |
| 温度标定 + 分条件策略 | 0.356 | 0.938 | 0.022 | 0.291 |

每条件只有 8 张标定图，这组数字用于验证“标定 → 按条件选门限 → 独立测试”的流程，不是出厂参数；测试集 ECE 高于标定集，说明类别偏移下标定不是万能药。

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
- API：注入 review 事件 → 保守决策；重复 `event_id` 幂等且不重复调用 LLM；携带 `image_b64` 返回 422；memory 入库→查询→压缩；
- topic：发布/订阅解析、错误层级拒绝；
- Edge-Mem：时间衰减三层边界、中文时间窗解析、RRF 融合、Token 截断、向量失败降级、无 LLM 的 JSON 时间轴；
- 仪器化：时钟偏移/同步标记、ECE/Brier、isotonic 单调性、conformal 集合、标定门限、SPC 均值偏移/标签漂移；
- 验收策略：false-accept 预算内选门限、样本不足回退默认、温度标定降低 NLL、按条件覆盖门限；
- 控制：连续 N 帧确认、最后一次检测后保持、stale → fault → reset、Modbus TCP FC5/FC1/FC3 往返；
- 事件时间：晚到事件 `late=true`、watermark、检索仍保留；
- 激励台：确定性渲染、重复性、CSV/Markdown 报告、ground truth 校验。

GitHub Actions 在 Python 3.10 和 3.12 上执行 `ruff check` 与 `pytest -q`。

---

## 已知限制

- 当前模型是通用 ImageNet MobileNetV1，不是工业缺陷/安全场景专用模型；演示的是架构，不是识别精度。
- v0.1 的 `/api/v1/events` 即时视图仍是有界进程内存储；v0.2 的 Edge-Mem 已把事件持久化到 SQLite，但默认向量索引是进程内的，重启后从 SQLite 重建。
- Edge-Mem 的哈希 embedding 不是语义模型；它保证离线可跑和测试可复现，真实语义检索应替换 `Embedder` 实现。
- 时间解析只覆盖项目会遇到的中文日期/相对时间/时段表达，不是通用 NLP；无法识别时回退最近 24 小时并标记 `fallback`。
- 压缩是破坏性的：warm/cold 的 raw 行会被删除。需要完整审计时，应在压缩前落对象存储或数据湖。
- 单消费者串行调用 LLM，换取背压和简单的限流；高并发需要 worker pool + 按设备保序。
- 未实现设备鉴权。MQTT 用户名密码/TLS 只留了配置位，`POST /api/v1/events` 是本地测试钩子。
- `metrics` 只统计应用层 payload，不含 TCP/TLS/MQTT 固定头；带宽结论已按此口径标注。
- 采样时钟默认是系统时钟包装；`ntp/ptp/gps` 只是显式标记，不包含真实授时驱动。要拿到可信绝对时间，需接 NTP/PTP 客户端并监控 offset。
- 标定需要自备工业场景的标注集；仓库只提供 isotonic/conformal 的实现和验收路径，不提供“已标定”的精度声明。
- v0.4 的 85 张公开图只是方法验证：每条件 8 张标定、9 张测试，选出的门限方差很大，不能直接当出厂参数。
- 温度标定是二分类置信度校准（correct vs incorrect），不是多分类 softmax 重标定；测试集 ECE 高于标定集，说明类别/条件偏移下校准会退化。
- 分条件策略目前是离线验收流程；运行时 `EdgeRunner` 仍用单一门限，除非调用方显式提供条件。不要把离线策略当成已上线的自适应门限。
- Modbus TCP 是纯标准库模拟从站，**不是安全 PLC**；联锁逻辑可验证，但不能替代硬件安全回路。
- 激励台默认场景是合成目标，`accuracy=n/a`；真实精度必须用 `--source-image --ground-truth` 的现场数据。
- SPC 是诊断信号，不是自动停线依据；告警阈值需要按现场基线调整。
- Python 代码是参考实现。迁移到 STM32 / ESP32 时，需要替换采集、推理和传输适配器；门限与协议逻辑可以复用，但不是"原样复制即可运行"。

---

## License

MIT，见 [LICENSE](LICENSE)。
