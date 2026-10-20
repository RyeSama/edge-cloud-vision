# 实测数据

`metrics-1000.json` 由以下命令生成（Windows，Python 3.12，MobileNetV1 INT8，合成源）：

```bash
python -m ecvision.edge --source synthetic --max-frames 1000 --fps 15 --metrics-out data/metrics-1000.json --log-every 200
```

```json
{
  "frames": 1000,
  "accepted": 0,
  "review": 1000,
  "elapsed_s": 66.594,
  "fps": 15.02,
  "json_payload_bytes": 563944,
  "raw_rgb_equivalent_bytes": 230400000,
  "jpeg_15kb_reference_bytes": 15000000,
  "reduction_vs_raw_rgb": 0.997552,
  "reduction_vs_jpeg_15kb": 0.962404
}
```

## 口径

- `raw_rgb_equivalent_bytes` = 320 × 240 × 3 × 1000，代表"每帧都传原始 RGB"的上界基线。
- `jpeg_15kb_reference_bytes` = 15 KB × 1000，代表常见 320×240 MJPEG 码率参考。
- `json_payload_bytes` **只统计 MQTT payload**，不含 TCP/TLS/MQTT 固定头；因此这是偏乐观的应用层数字。
- 合成源（移动方块 + 文字）对 ImageNet 预训练模型是分布外输入，置信度约 0.1%，所以 1000 帧全部进入 `review`。这组数据用于验证"带宽 + 兜底路径压力"，**不代表真实场景的 accept 比例**。统计 accept 比例请用真实视频/摄像头。

复现时如果使用真实视频，请把 `--source synthetic` 换成 `--source path/to/video.mp4 --loop`，其余参数不变。

## Edge-Mem 微基准

`edge-mem.json` 由以下命令生成（1000 条事件、3 小时跨度、5 类标签、2 台设备、默认哈希向量）：

```bash
python scripts/bench_memory.py --events 1000 --runs 50
```

关键结果：

| 指标 | 数值 |
|---|---|
| ingest | 2.62 s（1000 条） |
| compress | 1.14 s |
| 压缩后 hot / warm / cold | 56 / 64 / 333 |
| 压缩后向量数 | 120 |
| 查询 p50 / p95 | 2.94 ms / 5.36 ms |
| 时间窗内候选 | 87 |
| 返回条目 / 上下文 tokens | 10 / 509 |
| 降级 / 截断 | 否 / 否 |

口径：单进程、SQLite 文件库、进程内向量索引、50 次查询；不包含 HTTP 开销和 LLM 调用。
它不是容量规划结论，只用于证明默认离线路径能在毫秒级完成“时间过滤 → 向量召回 → RRF → 上下文组装”。

## v0.3 时间链

`clock-v03.json` 由以下命令生成（200 帧合成源、15 FPS、NTP 时钟、+50 ms 偏移、80 ms 截止期）：

```bash
python -m ecvision.edge --source synthetic --max-frames 200 --fps 15 \
  --clock-source ntp --clock-offset-ms 50 --clock-synced --deadline-ms 80 \
  --metrics-out docs/measurements/clock-v03.json
```

| 指标 | 数值 |
|---|---|
| capture → publish p50 / p95 / p99 | 8.825 / 12.183 / 21.750 ms |
| 推理 p50 / p95 | 8.46 / 11.90 ms |
| publish p50 | 0.241 ms |
| 帧间隔 p50 | 66.75 ms（配置 66.67 ms） |
| 帧间隔抖动 p95 | 11.50 ms |
| 截止期 80 ms 错失 | 0 / 200 |
| 丢帧 | 0 |
| 时钟 | `ntp`，synced 200 / unsynced 0 |

`stage_ms.read` 是**等下一帧的采集等待**（合成源按 15 FPS 限速），不是处理耗时；
capture 时间戳在 `read()` 返回后才取，所以 `latency_ms` 不包含这段等待。这是 v0.3 修正的一个口径问题：
更早的实现把限速 sleep 算进了端到端延迟，导致 p50 虚高到 66 ms。

## v0.3 仪器化激励台

`stimulus.md` 由以下命令生成（真实 MobileNetV1 INT8，8 个确定性场景 × 3 次）：

```bash
python scripts/bench_stimulus.py --out docs/measurements/stimulus.md
```

| 指标 | 结果 |
|---|---|
| 样本数 | 24 |
| accept / review | 0 / 24 |
| 置信度范围 | 0.0011–0.0018 |
| 单次推理 p50 | 8.11–11.76 ms（随场景变化） |
| accuracy | n/a（合成目标无 ground truth） |

合成目标对 ImageNet 模型是分布外输入，因此全部进入 `review`。这份报告证明的是**确定性、时序稳定性和门限行为**，不是识别精度；真实精度必须用带 ground truth 的现场图片跑 `--source-image --ground-truth`。

## v0.3 事件时间

`tests/test_event_time.py` 覆盖：3 条事件（10 分钟前、1 秒前、晚到 10 分钟）→ `events=3`、`late_events=1`，watermark 单调，检索结果保留 `late=true`。这是逻辑测试，不是吞吐 benchmark。
