# 文档索引

建议按下面的顺序读。只想跑起来，直接看根目录 [README](../README.md) 的“快速开始”。

| 文档 | 回答什么问题 |
|---|---|
| [architecture.md](architecture.md) | 模块怎么分层？线程/异步模型是什么？故障时会发生什么？怎么迁到 C++/MCU？ |
| [edge-mem.md](edge-mem.md) | v0.2 的时间衰减、SQLite/向量混合存储、RRF 检索、Token 预算和降级 |
| [instrumentation.md](instrumentation.md) | v0.3 的采样时钟、延迟/抖动/截止期、标定与 conformal、Modbus 联锁、激励台、SPC、事件时间 |
| [protocol.md](protocol.md) | MQTT topic、QoS、心跳、离线缓冲、去重、错误处理的具体约定 |
| [adr/0001-mqtt-vs-socket.md](adr/0001-mqtt-vs-socket.md) | 为什么主线选 MQTT 而不是裸 Socket/WebSocket/ZeroMQ？ |
| [measurements/README.md](measurements/README.md) | 1000 帧带宽实验的口径、命令和原始结果 |
| [../examples/minimal_loop.py](../examples/minimal_loop.py) | 模块 A 的最小可运行骨架 |

## 设计上的几个硬约束

1. **全链路不传原始图像。** `Event` 的 Pydantic 模型设置 `extra="forbid"`，携带 `image_b64` 之类字段会被直接拒绝；这不是文档约定，是运行时约束。
2. **低置信度不静默丢弃。** `>= 0.90` accept；`0.50–0.90` review(ambiguous)；`< 0.50` review(low_confidence)。工业场景里“模型没见过”本身可能是风险。
3. **LLM 失败必须可见。** 未配置 Key、超时、非法 JSON 都降级为 `rule_fallback / unknown / request_human_review`，并在 `/healthz` 和 `/metrics` 里留下计数，不假装成功。
4. **带宽数字必须带基线。** 同时报告相对原始 RGB 和相对 15 KB/帧 MJPEG 的降幅；只统计 MQTT payload，不含 TCP/TLS 固定头。
5. **记忆检索必须可降级。** 时间窗先做 SQLite 硬过滤；向量检索失败回退文本查询并标记 `degraded`；没有 LLM 时返回 JSON 时间轴，而不是 500。
6. **测量结论必须带口径。** 延迟区分 capture→emit 与 capture→publish；置信度只有经过标定才称为概率；时间戳区分 `synced/unsynced`；控制输出必须经过联锁，不能把模型结果直接当执行信号。

## 复现实验

```bash
# 1) 依赖与模型
pip install -e ".[litert,dev]"
python scripts/download_model.py

# 2) 无 broker 的最小闭环
python examples/minimal_loop.py

# 3) 1000 帧带宽实验（不需要 broker，dry-run 也能出 metrics）
python -m ecvision.edge --source synthetic --max-frames 1000 --fps 15 \
  --metrics-out data/metrics-1000.json --log-every 200

# 4) 端到端（需要 Mosquitto）
docker compose up -d mosquitto
python -m ecvision.cloud
python -m ecvision.edge --source synthetic --max-frames 100

# 5) v0.3 时间链 + 控制闭环（不需要 broker）
python -m ecvision.edge --source synthetic --max-frames 200 \
  --clock-source ntp --clock-offset-ms 50 --deadline-ms 80 \
  --metrics-out data/clock.json
python -m ecvision.edge --source synthetic --max-frames 300 \
  --interlock --modbus --modbus-port 15020

# 6) v0.3 激励台（需要模型）
python scripts/bench_stimulus.py --out docs/measurements/stimulus.md
```

## 已知限制

见根 README 的“已知限制”一节。最重要的三条：模型是通用 ImageNet 分类器而非工业专用模型；
v0.1 的即时事件视图是进程内存（v0.2 Edge-Mem 已持久化到 SQLite）；端侧离线缓冲在进程崩溃时会丢。
写文档时不要把这些说成已解决。
