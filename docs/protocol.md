# MQTT 通信协议 v1.1

本文件是端侧（模块 A/B）与边缘网关（模块 C）之间的线上契约。JSON 的结构以
[`contracts/event.schema.json`](../contracts/event.schema.json) 为准；本文件解释
**为什么这样设计**以及**出错时怎么办**。

## 1. 传输参数

| 项 | 值 | 说明 |
|---|---|---|
| 协议 | MQTT 3.1.1 | 兼容 Mosquitto / EMQX / 阿里云 IoT |
| QoS | 1（至少一次） | 允许重复，不允许静默丢失；云端按 `event_id` 去重 |
| Keepalive | 30 s | 由 paho 维护 PINGREQ |
| 应用心跳 | 10 s | 独立的 retained `status` 消息，便于人看和告警 |
| Clean Session | 端侧 `false`，云端 `true` | 端侧保留会话，断线期间 QoS1 可续传 |
| 单条消息上限 | 64 KB | 云端硬限制；正常事件 < 1 KB |
| 字符编码 | UTF-8 | `ensure_ascii=False`，中文标签可直接传输 |

## 2. Topic 设计

```text
ecv/v1/{device_id}/event     端 → 云   事件，QoS1，不 retain
ecv/v1/{device_id}/status    端 → 云   在线状态/心跳，QoS1，retain
ecv/v1/{device_id}/command   云 → 端   预留控制通道，QoS1，不 retain
```

- `ecv` 是项目命名空间，`v1` 是协议大版本；不兼容变更升 `v2`，不复用旧 topic。
- `{device_id}` 必须匹配 `^[A-Za-z0-9_.:-]+$`，与 payload 内的 `device_id` 一致。
  云端会校验 topic 与 payload，不一致直接丢弃并计入 `invalid`。
- 使用通配符订阅 `ecv/v1/+/event`。**不要**用 `ecv/#`，否则会把 status 也送进事件管线。
- `status` 使用 retain：新订阅者立即知道设备最后状态；LWT 保证异常掉线也会变成 `offline`。

## 3. 事件消息

### 3.1 示例

```json
{
  "schema_version": "1.1",
  "event_id": "8f14e45f-ea2b-4c1e-9f0a-3d2b6c7a1e90",
  "device_id": "edge-cam-01",
  "seq": 42,
  "ts": "2026-10-19T09:30:00.123456Z",
  "source": {"width": 320, "height": 240, "fps": 15.0},
  "inference": {
    "model": "mobilenet_v1_1.0_224_quant",
    "label": "laptop",
    "label_id": 620,
    "confidence": 0.62,
    "latency_ms": 19.7,
    "top_k": [
      {"label": "laptop", "confidence": 0.62},
      {"label": "notebook", "confidence": 0.11},
      {"label": "desktop computer", "confidence": 0.07}
    ]
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

### 3.2 字段语义

| 字段 | 必填 | 约束 | 用途 |
|---|---|---|---|
| `schema_version` | 是 | `"1.0"` 或 `"1.1"`；v0.3 默认 `"1.1"` | 版本演进；`1.1` 新增可选 `sample` |
| `sample` | 否 | v0.3 采样元数据 | 采集/推理/发布时间戳、截止期、时钟同步状态 |
| `event_id` | 是 | UUID4 字符串 | 幂等键；重发必须复用同一个值 |
| `device_id` | 是 | 1–64 字符 | 设备身份；与 topic 校验 |
| `seq` | 是 | ≥ 0，设备内单调递增 | 发现断线空洞、估算丢包 |
| `ts` | 是 | RFC3339，必须带时区 | 事件时间；不要用本地 naive 时间 |
| `source` | 是 | 宽高 > 0，fps (0,240] | 证明采集分辨率，便于算带宽 |
| `inference` | 是 | 置信度 [0,1]，top_k ≤ 5 | 首层推理结果 |
| `gate` | 是 | `review_threshold ≤ accept_threshold` | 门限决策与当时阈值，便于回溯 |

**禁止字段**：任何图像、视频、base64、特征向量。Pydantic 的 `extra="forbid"` 和
JSON Schema 的 `additionalProperties: false` 会拒绝未声明字段。这是"不传视频"
这一卖点的技术保证，而不是口头承诺。

### 3.3 门限语义

```text
confidence >= accept_threshold            -> decision=accept, reason=high_confidence
review_threshold <= confidence < accept   -> decision=review, reason=ambiguous
confidence < review_threshold             -> decision=review, reason=low_confidence
```

默认 `accept=0.90`、`review=0.50`。低置信度**不丢弃**：安全场景中漏检的代价高于
一次额外 LLM 调用。若业务要求静默丢弃，应在云端策略层显式配置，而不是在端侧悄悄做。

## 4. 状态消息

```json
{
  "schema_version": "1.1",
  "device_id": "edge-cam-01",
  "state": "online",
  "ts": "2026-10-19T09:30:10.000000Z",
  "uptime_s": 123.4,
  "events_published": 987,
  "buffered": 0
}
```

`state` 取值 `online` / `offline`。`offline` 由两种方式产生：

1. 正常退出：`MqttPublisher.stop()` 主动发布 retained `offline`；
2. 异常掉线：Broker 根据 connect 时注册的 LWT 发布 retained `offline`。

## 5. 可靠性与去重

- **至少一次**：发布端 QoS1，订阅端 QoS1；本仓库的 Mosquitto compose 配置开启
  `persistence true`，但消息最终是否落盘仍取决于 Broker 配置。
- **重复是正常现象**：断线重连、PUBACK 丢失都会导致重复。云端 `EventStore.add`
  以 `event_id` 为键，重复消息不会覆盖已经附加的 LLM 决策。
- **顺序**：单设备内 MQTT 保序；跨设备不保证。云端单消费者串行处理，换取简单背压。
- **离线缓冲**：端侧发布失败或未连接时进入 `deque(maxlen=500)`；恢复连接后按 FIFO
  补发。缓冲满时丢最旧并计数，不阻塞采集线程。
- **会话**：两端都使用 `clean_session=False` + 固定 client_id。端侧断线期间 QoS1
  可续传；云端短暂重启时，**只有 Broker 开启持久化且 session 未过期**才会保留其
  订阅队列并在重连后补发。云端固定 client_id 意味着同一 id 不能并发运行多个实例
  （本地演示可接受）。重复事件由 `event_id` 幂等处理，不会重复调用 LLM。
  > 实测提醒：`amqtt` 测试 broker 约 3 秒就过期 MQTT 3.1.1 session，不能用来证明
  > “云端离线队列”能力；该能力请用 Mosquitto 验证。

## 6. 错误处理约定

| 场景 | 端侧 | 云端 |
|---|---|---|
| Broker 不可达 | 缓冲，指数退避重连（1–30 s） | 持续重连，`/healthz.mqtt_connected=false` |
| payload 非 JSON | 不会发生（本地序列化） | 计数 `invalid`，丢弃，不影响后续 |
| 字段缺失/越界 | 本地 Pydantic 构造时就会失败 | `invalid`，记录 `last_error` |
| topic 与 device_id 不一致 | — | 拒绝，防止伪造/串号 |
| 消息 > 64 KB | — | 拒绝，计数 |
| LLM 失败 | — | `rule_fallback`，事件仍可查询 |
| 云端队列满 | — | 丢弃 + `ecv_events_dropped_total` |

## 7. 安全边界

当前是**本地演示配置**：`allow_anonymous true`、明文 1883。生产至少需要：

- Broker 用户名/密码或客户端证书；
- TLS 8883，关闭匿名；
- 每设备独立 ACL：只能 publish 自己的 `ecv/v1/{device_id}/event` 和 `status`；
- 云端 `POST /api/v1/events` 测试钩子关闭或加鉴权。

`MqttConfig` 已预留 `username` / `password` 字段，接上 TLS 只需在 `mqtt.Client`
上调用 `tls_set()`；本项目不把"本地能跑"包装成"生产安全"。

## 8. 版本演进

- 新增可选字段：小版本升位（如 `1.0` → `1.1` 的 `sample`），旧消费者忽略；本项目
  `extra="forbid"`，所以消费端需同步升级模型，或把新字段明确设为可选。
- 删除/改名/改变语义：升 `2.0`，同时支持双订阅一段时间。
- 端侧与云端应在握手/状态消息中带 `schema_version`，便于灰度。
