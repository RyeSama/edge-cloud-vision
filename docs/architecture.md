# 架构说明

## 1. 组件与依赖方向

```text
                     contracts.py (Event / Decision)
                        ▲                 ▲
                        │                 │
        ┌───────────────┴──┐          ┌───┴────────────────┐
        │ edge             │          │ cloud              │
        │ sources          │          │ subscriber         │
        │ preprocess       │          │ store              │
        │ classifier       │          │ decision ──► LLM   │
        │ gate             │          │ main (FastAPI)     │
        │ metrics          │          │   │                │
        │ pipeline ────────┼──┐       │   ▼                │
        └──────────────────┘  │       │ memory (Edge-Mem)  │
                              │       │  repository(SQLite)│
                     EventPublisher   │  vector index      │
                     (Protocol)       │  retriever / RRF   │
                  ┌───────────┴───────┐│  context builder   │
                  │ DryRunPublisher   ││  answerer ──► LLM  │
                  │ MqttPublisher ────┼┴─ MQTT ────────────┘
                  └───────────────────┘
```

规则：

- `edge` 不 import `cloud`，`cloud` 不 import `edge`；共享的只有 `contracts`。
- `memory` 只 import `contracts` 和自身 Protocol，不 import `cloud`；`cloud.main` 负责把
  `CloudSettings` 结构化传给 `MemoryService.from_settings`，方向是 cloud → memory。
- `pipeline` 只依赖 `FrameSource` / `Classifier` / `EventPublisher` 三个 Protocol。
- `cloud.decision` 只依赖 `Event` / `Settings` / `httpx`，可以脱离 FastAPI 单测。
- `memory` 的存储和检索都通过 `EventRepository` / `VectorIndex` / `Embedder` /
  `Answerer` Protocol 注入；默认实现可离线跑，Chroma 是可选适配器。
- `instrument` 只依赖标准库，负责时钟、标定和 SPC；不 import `edge`/`cloud`。
- `control` 只依赖标准库，Modbus TCP 从站和联锁状态机可以脱离视觉单测。
- `bench` 只依赖 `Classifier` Protocol 和 numpy/OpenCV，用来产生确定性激励，不 import `EdgeRunner`。
- `edge.pipeline` 是唯一把采集、推理、门限、仪器化、联锁串起来的地方；它通过构造参数接收
  `SamplingClock`、`CalibrationGate`、`SpcMonitor`、`InterlockController`，不直接 new 这些对象。

## 2. 运行时序（正常路径）

```text
Edge                Broker                  Cloud
 │                    │                       │
 │ connect + LWT      │                       │
 ├───────────────────►│                       │
 │ PUB status online  │                       │
 ├───────────────────►│                       │
 │                    │◄──────────────────────┤ subscribe ecv/v1/+/event
 │ PUB event (QoS1)   │                       │
 ├───────────────────►│──────────────────────►│ validate
 │                    │                       │ store.add
 │                    │                       │ memory.ingest
 │                    │                       │ if review: LLM
 │                    │                       │ memory.attach_decision
 │ PUB status (10s)   │                       │
 ├───────────────────►│                       │
 │ ×  crash           │                       │
 │                    │ PUB LWT offline       │
 │                    ├──────────────────────►│
```

## 3. 线程/异步模型

```text
Edge process                          Cloud process
┌──────────────────────┐              ┌──────────────────────────────┐
│ main thread          │              │ uvicorn event loop            │
│  capture→infer→gate  │              │  FastAPI handlers             │
│  publisher.publish   │              │  _consume coroutine           │
│                      │              │       ▲                       │
│ paho network thread  │              │       │ call_soon_threadsafe  │
│  reconnect/heartbeat │              │ paho network thread           │
│ heartbeat thread     │              │  _on_message: validate only   │
└──────────────────────┘              └──────────────────────────────┘
```

关键约束：

- **paho 回调里不做 I/O、不调 LLM、不 sleep**。云端回调只做 JSON 解析 + Pydantic
  校验 + `loop.call_soon_threadsafe(queue.put_nowait, event)`。
- LLM 调用全部在 asyncio 消费协程里，串行执行。这提供天然背压：队列满时直接丢弃并
  计数，而不是让 MQTT 网络线程阻塞、心跳超时、整条链路雪崩。
- 端侧推理在单线程串行执行；TFLite interpreter 不是线程安全的，`TFLiteClassifier`
  内部有锁，为未来多路采集预留正确性。

## 4. 一致性/幂等

- 端侧 `seq` 每进程从 0 开始；云端只记录，不强制连续（重启会归零）。若要严格检测
  空洞，应把 `(device_id, boot_id, seq)` 作为键，当前版本未实现。
- `event_id` 由端侧生成，重发复用；云端 `EventStore.add` 先查 `event_id`，存在则
  直接返回，不覆盖已有 `Decision`。这是 QoS1 至少一次语义下的必要设计。
- 决策写入是"先存事件、后 attach 决策"；查询时可能短暂看到 `decision=null`，
  这是有意的：不阻塞事件可见性。

## 5. 时间语义

- `Event.ts` 是**采集时间**（capture wall clock），不是发出时间。
- `SampleInfo.capture_mono_ns` 来自 `perf_counter_ns`，用于延迟/抖动；`capture_wall_ns` 来自墙钟，用于跨设备比较。
- `Event.sample.end_to_end_ms` 是 capture → emit；`RunMetrics.latency_ms` 是 capture → publish_done，二者口径不同。
- `ClockInfo.synced=false` 时事件仍然发出，但指标单独计数 `unsynced_frames`；不把未同步时间当可信时间。
- Edge-Mem 的 `capture_time` 是事件时间，`ingest_time` 是入库时间；`watermark` 之前超过允许迟到窗口的事件标记 `late=true`，但仍然保留。

## 6. 故障模式

| 故障 | 检测 | 恢复 | 数据影响 |
|---|---|---|---|
| 端侧进程崩溃 | Broker LWT | 重启后重新连接 | 内存缓冲丢失（未持久化） |
| 网络闪断 | paho keepalive | 1–30 s 退避重连 | 缓冲内事件补发 |
| Broker 重启 | paho 断开回调 | 自动重连 | 端侧缓冲补发；Broker 持久化 + session 未过期时云端队列也可补发 |
| 云端重启 | — | 重新订阅；v0.1 即时视图清空，Edge-Mem 从 SQLite 恢复 | v0.1 即时视图丢失；Edge-Mem 事件保留 |
| Edge-Mem SQLite 初始化失败 | exception | memory API 返回 503，v0.1 主链路继续 | 记忆不可用，事件仍可接收 |
| 向量索引失败 | exception | 回退 SQLite 文本查询，`degraded=true` | 召回率下降，不丢事件 |
| 压缩期间崩溃 | 启动时按时间重跑 | 幂等 upsert / 已删除 raw 不再处理 | warm/cold 可能重复摘要，不会丢热数据 |
| 时钟未同步 | `ClockInfo.synced=false` | 继续采集，指标标记 | 时间不可用于跨设备对齐 |
| 截止期错失 | `end_to_end_ms > deadline_ms` | 计数并继续 | `deadline_misses` / `deadline_miss_rate` 上升 |
| Modbus 端口占用 | `OSError` at bind | CLI 报错退出或禁用 `--modbus` | 不影响无控制模式 |
| 联锁 stale/watchdog | 超过 `stale_after_s` 无样本 / 心跳 | 进入 `fault`，`fail_safe=true`，锁存 | 需要 `reset` 才恢复 |
| 标定 profile 缺失 | `CalibrationGate(profile=None)` | 使用原始置信度 | 继续运行，但不声称已标定 |
| SPC 告警 | EWMA/CUSUM/JS 超限 | 记录 `spc.alerts`，不阻断 | 用于漂移诊断 |
| LLM 超时 | httpx timeout | 下一次请求重试 | 当前事件降级为人工复核 |
| LLM 限流 429 | HTTPStatusError | 不重试，降级 | 计数 fallback |
| 队列满 | `QueueFull` | 丢弃最晚到达的事件 | 计数 dropped |
| 畸形消息 | Pydantic | 丢弃并继续 | 计数 invalid |

## 7. 带宽口径

`RunMetrics` 只统计应用层 JSON 字节，不含：

- MQTT 固定头（2 B）+ 剩余长度 + topic（约 20 B）+ QoS1 报文标识（2 B）；
- TCP/IP 头（40 B）；
- TLS 记录头（若启用）。

即使全部加上，单条约增加 60–100 B，相对 600–800 B payload 约 10–15%，不改变
数量级结论。README 明确标注了口径，避免"99%"被质疑时无法解释。

原始 RGB 基线：`frames × width × height × 3`。这是未压缩的传感器数据，作为上界；
JPEG 基线按 15 KB/帧参考值，作为更严格的对照。两个都报告，才经得起追问。

## 8. 向 C++/MCU 迁移的映射

| Python | C++/MCU | 说明 |
|---|---|---|
| `OpenCVSource.read()` | `esp_camera_fb_get()` / DCMI+DMA | 输出 RGB565/JPEG，需转 RGB888 |
| `center_crop_square` + `to_rgb_input` | 定点 resize / 硬件缩放 | 注意 uint8 饱和与通道序 |
| `TFLiteClassifier` | TFLite Micro `MicroInterpreter` | 算子集需与模型匹配；INT8 量化参数从 tensor 读取 |
| `gate.evaluate` | 两个整数比较 | 阈值可用 `uint16` 千分比，避免浮点 |
| `encode_event` | 手写 JSON 序列化 / cJSON | 固定字段顺序，避免动态分配 |
| `MqttPublisher` | esp-mqtt / PubSubClient / 4G AT | QoS1、LWT、心跳需确认模组支持 |
| `RunMetrics` | 计数器 + 定期上报 | 注意 32 位溢出 |

迁移时**不要**把 Python 的 Pydantic 模型照搬成大量类；C++ 端只需要一个固定大小的
struct + 序列化函数。协议字段和门限语义才是需要保持一致的部分。

## 9. 已知瓶颈与下一步

1. **单消费者**：LLM 串行，吞吐受限于单次调用延迟。下一步：按设备哈希分片 +
   每设备保序，或引入本地小模型做第二层。
2. **v0.1 即时视图仍是进程内存**：重启丢失；持久化由 Edge-Mem SQLite 承担。下一步：
   把 `/api/v1/events` 也切到 SQLite 查询，或引入 ClickHouse。
3. **无鉴权/TLS**：本地演示配置。下一步：mTLS + 每设备 ACL。
4. **门限仍缺工业标定集**：`CalibrationGate` 已支持 isotonic/conformal，但仓库没有
   工业缺陷/安全场景的标注数据；当前只能证明方法和降级路径。
5. **无端侧持久化**：断电丢缓冲。下一步：环形 flash 日志（LittleFS）。
6. **Modbus 模拟器不是安全 PLC**：它验证协议、时序和联锁逻辑，不能替代安全回路。
7. **激励台是合成场景**：证明可复现性和时序稳定性，不证明识别精度；真实精度需要
   带 ground truth 的现场数据。
