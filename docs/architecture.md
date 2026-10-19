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
        │ metrics          │          │                    │
        │ pipeline ────────┼──┐       │                    │
        └──────────────────┘  │       └────────────────────┘
                              │                 ▲
                     EventPublisher             │
                     (Protocol)                 │
                  ┌───────────┴───────┐         │
                  │ DryRunPublisher   │         │
                  │ MqttPublisher ────┼── MQTT ─┘
                  └───────────────────┘
```

规则：

- `edge` 不 import `cloud`，`cloud` 不 import `edge`；共享的只有 `contracts`。
- `pipeline` 只依赖 `FrameSource` / `Classifier` / `EventPublisher` 三个 Protocol。
- `cloud.decision` 只依赖 `Event` / `Settings` / `httpx`，可以脱离 FastAPI 单测。

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
 │                    │                       │ if review: LLM
 │                    │                       │ attach decision
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

## 5. 故障模式

| 故障 | 检测 | 恢复 | 数据影响 |
|---|---|---|---|
| 端侧进程崩溃 | Broker LWT | 重启后重新连接 | 内存缓冲丢失（未持久化） |
| 网络闪断 | paho keepalive | 1–30 s 退避重连 | 缓冲内事件补发 |
| Broker 重启 | paho 断开回调 | 自动重连 | 端侧缓冲补发；Broker 持久化 + session 未过期时云端队列也可补发 |
| 云端重启 | — | 重新订阅；内存库清空 | **事件库丢失**（已知限制） |
| LLM 超时 | httpx timeout | 下一次请求重试 | 当前事件降级为人工复核 |
| LLM 限流 429 | HTTPStatusError | 不重试，降级 | 计数 fallback |
| 队列满 | `QueueFull` | 丢弃最晚到达的事件 | 计数 dropped |
| 畸形消息 | Pydantic | 丢弃并继续 | 计数 invalid |

## 6. 带宽口径

`RunMetrics` 只统计应用层 JSON 字节，不含：

- MQTT 固定头（2 B）+ 剩余长度 + topic（约 20 B）+ QoS1 报文标识（2 B）；
- TCP/IP 头（40 B）；
- TLS 记录头（若启用）。

即使全部加上，单条约增加 60–100 B，相对 600–800 B payload 约 10–15%，不改变
数量级结论。README 明确标注了口径，避免"99%"被质疑时无法解释。

原始 RGB 基线：`frames × width × height × 3`。这是未压缩的传感器数据，作为上界；
JPEG 基线按 15 KB/帧参考值，作为更严格的对照。两个都报告，才经得起追问。

## 7. 向 C++/MCU 迁移的映射

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

## 8. 已知瓶颈与下一步

1. **单消费者**：LLM 串行，吞吐受限于单次调用延迟。下一步：按设备哈希分片 +
   每设备保序，或引入本地小模型做第二层。
2. **内存事件库**：重启丢失。下一步：SQLite（WAL）+ 保留策略，或 ClickHouse。
3. **无鉴权/TLS**：本地演示配置。下一步：mTLS + 每设备 ACL。
4. **固定阈值**：不同场景最优阈值不同。下一步：按类别/场景配置，或基于置信度
   校准（temperature scaling）后再门限。
5. **无端侧持久化**：断电丢缓冲。下一步：环形 flash 日志（LittleFS）。
