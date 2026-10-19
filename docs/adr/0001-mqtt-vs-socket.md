# ADR 0001：本地演示为什么选 MQTT，而不是裸 Socket

- 状态：Accepted
- 日期：2026-10-19
- 决策者：项目作者
- 相关：[`docs/protocol.md`](../protocol.md)、[`src/ecvision/transport/mqtt.py`](../../src/ecvision/transport/mqtt.py)

## 背景

模块 A 与模块 C 需要一条**纯本地可测试**、但又能映射到真实物联网部署的通信链路。
候选方案：

1. **裸 TCP/UDP Socket + 自定义帧头**；
2. **WebSocket**；
3. **MQTT 3.1.1 + Mosquitto**。

约束：

- 只传结构化 JSON，不传视频，因此单条消息 < 1 KB，吞吐要求很低（15 FPS × 1 条）；
- 需要心跳、断线重连、离线补发、在线状态；
- 开发者使用 Windows，没有 Docker 也应该能跑；
- 代码要能让新用户在 30 分钟内复现；
- 未来要能平移到 ESP32 / STM32 + 4G 模组。

## 决策

**主链路使用 MQTT**，并把传输抽象为 `EventPublisher` Protocol；Socket 仅作为教学/对比
方案保留在文档中，不进入主链路。

## 理由

| 维度 | 裸 Socket | WebSocket | MQTT |
|---|---|---|---|
| 断线重连 | 自己写，容易写出半开连接 | 自己写 | paho 内置指数退避 |
| 心跳/保活 | 自己定义 | Ping/Pong 需处理 | Keepalive + 应用层 status |
| 离线补发 | 自己维护队列和确认 | 同左 | QoS1 + persistent session |
| 设备异常离线检测 | 依赖 TCP 超时，慢 | 依赖 TCP/代理 | LWT 遗嘱，Broker 代发 |
| 一对多/多消费者 | 每端自己 accept | 需要网关 | Broker 天然支持 |
| 可观测/调试 | tcpdump + 自己解析 | 浏览器 DevTools | `mosquitto_sub` 一条命令 |
| 设备身份/ACL | 自己实现 | 自己实现 | Broker 原生 |
| 协议开销 | 最低 | 中（HTTP 升级 + 帧头） | 低（固定头 2 B + topic） |
| 本地启动成本 | 零依赖 | 需 WS 服务 | 需 Broker（Docker/安装包） |
| 上云/4G/NAT | 需公网端口/打洞 | 需反代 | 1883/8883 直连，云厂商托管 |

关键判断：

- 项目**已经通过端侧过滤把数据量压到极低**，此时 MQTT 的协议开销（每条约几十字节）
  相对于 600–800 B 的 JSON 可以忽略；用裸 Socket 省下的这点字节没有工程意义。
- 真正难的是**可靠性语义**：重连、补发、去重、离线检测。这些恰好是 MQTT 已经解决、
  而手写 Socket 最容易出错的部分。选择 MQTT 是把复杂度放在正确的地方。
- LWT + retained status 让"设备掉线"在演示里**可见**：`mosquitto_sub` 能看到
  `offline`，这比在日志里打印 `ConnectionResetError` 更有说服力。
- ESP32 有成熟的 `esp-mqtt` / PubSubClient；STM32 + 4G 模组也通常走 MQTT AT 指令。
  选型与目标岗位的真实技术栈一致。

## 被否决的方案

### 裸 Socket

适合：固定机房内、超低延迟、自定义二进制协议、需要精确控制每一字节。
不适合本项目：要自己实现重连/心跳/补发/去重，代码量和 bug 面远大于收益；现场演示时
也无法用现成工具观察。**保留为未来"高吞吐视频特征流"的扩展点**，而不是当前主链路。

### WebSocket

适合：浏览器直连、需要穿透 80/443 的场景。本项目云端是 Python 服务，没有浏览器
实时推流需求；引入 WS 只增加一层协议，不解决离线补发和设备状态问题。

### ZeroMQ / nanomsg

性能好，但 broker-less 模式要求双方同时在线，缺少 LWT/retained 这类设备管理语义，
且云端多消费者扩展不如 MQTT 直观。

## 本地测试方案

### 方案 A：Docker（推荐，跨平台一致）

```bash
docker compose up -d mosquitto
docker compose logs -f mosquitto
```

`docker-compose.yml` 挂载 [`deploy/mosquitto/mosquitto.conf`](../../deploy/mosquitto/mosquitto.conf)，
开启 1883、匿名访问、持久化。仅用于本地演示。

### 方案 B：Windows 原生安装

1. 从 <https://mosquitto.org/download/> 安装 Windows x64 版本；
2. 新建 `mosquitto.conf`：

   ```conf
   listener 1883 127.0.0.1
   allow_anonymous true
   persistence true
   persistence_location C:\mosquitto\data\
   log_dest stdout
   ```

3. 启动：

   ```powershell
   & "C:\Program Files\mosquitto\mosquitto.exe" -c .\deploy\mosquitto\mosquitto.conf -v
   ```

4. 另开终端订阅：

   ```powershell
   & "C:\Program Files\mosquitto\mosquitto_sub.exe" -h 127.0.0.1 -t "ecv/v1/+/event" -v
   & "C:\Program Files\mosquitto\mosquitto_sub.exe" -h 127.0.0.1 -t "ecv/v1/+/status" -v
   ```

### 方案 C：不装 Broker 的降级路径

`python -m ecvision.edge --dry-run` 直接把 JSON 打到 stdout，用于验证模块 A；
云端可用 `POST /api/v1/events` 注入事件，用于验证模块 C。这样即使环境里没有
Mosquitto/Docker，也能分别演示两层逻辑；但**不能**声称验证了 MQTT 链路。

## 后果

正面：

- 可靠性能力（QoS1、LWT、心跳、重连、离线缓冲）直接复用成熟实现；
- 演示可用 `mosquitto_sub` / MQTTX 观察，降低验证成本；
- topic/JSON 契约与语言无关，C++ 端只需实现同样的 publish/subscribe。

负面：

- 多一个 Broker 进程，首次运行需要 Docker 或安装 Mosquitto；
- MQTT 的"至少一次"要求云端做幂等，已用 `event_id` 去重解决；
- 匿名明文配置不能直接上生产，已在协议文档中列为安全边界。

## 复审条件

出现以下情况时重新评估：

- 需要传输图像/特征向量且带宽成为瓶颈（考虑 QUIC/自定义二进制 + 压缩）；
- 需要端到端 < 10 ms 的硬实时控制（MQTT 的排队/重连语义不合适）；
- 设备数量 > 10 万，Broker 成为单点（考虑 EMQX 集群或云 IoT 平台）。
