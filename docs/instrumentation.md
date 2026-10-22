# v0.3 仪器化与闭环

v0.3 不追求“更多识别类别”，而是把 v0.2 的视觉链路往**测量仪器**方向补：

- 时间基准可解释：采集时间、发出时间、端到端延迟、抖动、截止期、丢帧；
- 置信度可标定：ECE/Brier、beta/温度/等渗回归、conformal prediction set；
- 输出可闭环：Modbus TCP 寄存器映射 + 去抖/保持/故障安全的联锁状态机；
- 验证可复现：确定性激励矩阵、SPC 漂移监控、事件时间 watermark。

对应代码：

| 能力 | 代码 |
|---|---|
| 采样时钟 | `src/ecvision/instrument/clock.py` |
| 标定与 conformal | `src/ecvision/instrument/calibration.py` |
| 统计过程控制 | `src/ecvision/instrument/spc.py` |
| Modbus TCP 从站 | `src/ecvision/control/modbus.py` |
| 联锁状态机 | `src/ecvision/control/interlock.py` |
| 激励台 | `src/ecvision/bench/stimulus.py` |
| 端侧接线 | `src/ecvision/edge/pipeline.py`、`src/ecvision/edge/__main__.py` |

## 1. 时间链

`SamplingClock` 同时给出两个读数：

- `mono_ns`：`perf_counter_ns`，单调，不受 NTP 跳变影响，用于延迟/抖动/截止期；
- `wall_ns`：墙钟，用于跨设备比较和落库。

`Event.ts` 是**采集时刻**，不是发出时刻；`SampleInfo` 保存端侧处理链路：

```text
capture_mono_ns → read_done_mono_ns → infer_start_mono_ns
→ infer_done_mono_ns → emit_wall_ns
```

`publish_done_mono_ns` 是可选字段，正常事件在发布前构造，所以它是 `null`；发布完成时刻由
`RunMetrics` 在 `publisher.publish()` 返回后记录，用于 `latency_ms`（capture→publish）。

`ClockInfo` 记录 `source`（system/monotonic/ntp/ptp/gps）、`boot_id`、`offset_ms`、`synced`。没有同步时 `synced=false`，指标里单独计数 `unsynced_frames`，不会假装时间是准的。

`edge/metrics.py` 输出：

```json
{
  "latency_ms": {"p50": 18.2, "p95": 25.4, "p99": 31.0, "max": 40.1},
  "stage_ms": {"read": 2.1, "infer": 12.4, "publish": 0.8},
  "capture_dt_us": {"p50": 66666.7, "p95": 70000.0},
  "jitter_us": 1200.0,
  "deadline_ms": 66.7,
  "deadline_misses": 0,
  "deadline_miss_rate": 0.0,
  "dropped_frames": 0,
  "drop_rate": 0.0,
  "clock": {"source": "ntp", "synced_frames": 1000, "unsynced_frames": 0}
}
```

注意：`latency_ms` 是 capture→publish，`Event.sample.end_to_end_ms` 是 capture→emit（发布前）；两者不要混用。
`stage_ms.read` 是**等下一帧的采集等待**（帧率限速或摄像头阻塞），capture 时间戳在 `read()` 返回后才取，
所以它不计入 `latency_ms`。这个口径修正本身是 v0.3 的一部分：早期实现把限速 sleep 算进延迟，p50 会虚高。

### 注入坏时钟

```bash
python -m ecvision.edge --source synthetic --max-frames 100 \
  --clock-source ntp --clock-offset-ms 50 --deadline-ms 80 \
  --metrics-out data/clock-offset.json
```

`--clock-unsynced` 会把 `clock.synced` 强制为 false，用来验证仪表盘/告警是否会区分“时间不可信”。

## 2. 标定与不确定度

原始 softmax 置信度不是概率。`CalibrationGate` 支持：

1. `CalibrationProfile`：identity、isotonic、temperature 或 beta 映射；
2. `ConformalProfile`：split-conformal 的 `qhat`，生成预测集合；
3. 集合大小 > 1 直接判 `review`，即使 top-1 置信度很高。

标定方法的选择：

- `temperature`：单参数，最稳，适合先跑通；
- `beta`：三参数 `sigmoid(a·log(s) − b·log(1−s) + c)`，能处理“高分段过度自信、低分段反而不自信”的不对称，适合小标定集；
- `isotonic`：非参数、单调，但样本少时容易阶梯化，适合大标定集；
- `identity`：基线，不做校准。

`beta` 来自 Kull et al., *Beta calibration: a well-founded and easily implemented improvement on logistic calibration for binary classifiers*, AISTATS 2017。这里把它用于“top-1 是否正确”的二分类置信度，不是重新做 1000 类 softmax。

拟合脚本：

```bash
python scripts/fit_calibration.py \
  --input data/calibration.jsonl \
  --method beta \
  --out data/calibration.json \
  --conformal-out data/conformal.json \
  --alpha 0.1
```

输入每行：

```json
{"confidence": 0.93, "correct": true}
{"confidence": 0.62, "correct": false, "score": 0.78}
```

`score` 省略时用 `1 - confidence` 作为非一致性分数。输出是严格 JSON，可直接给 CLI：

```bash
python -m ecvision.edge --source samples/warehouse.mp4 --max-frames 500 \
  --calibration-profile data/calibration.json \
  --conformal-profile data/conformal.json \
  --metrics-out data/calibrated.json
```

门限行为：

```text
conformal set size > 1        → review / ambiguous（优先于置信度）
calibrated >= 0.90            → accept
0.50 <= calibrated < 0.90     → review / ambiguous
calibrated < 0.50             → review / low_confidence
```

**不要**在没有标定集时声称“置信度 0.9 = 准确率 90%”。本仓库只提供方法和可执行路径，不提供工业标定数据。

### 2.1 分条件验收（v0.4）

全局门限把 clean、blur、occlusion、lowlight、downscale 当成同一种分布，结果通常是“clean 太保守、blur 太激进”。`AcceptancePolicy` 在 false-accept 预算下，为每个条件单独选 accept 门限：

```bash
python scripts/split_manifest.py --input data/manifest.csv \
  --calibration-out data/manifest-cal.csv --test-out data/manifest-test.csv
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

`select_threshold` 从高到低扫描候选门限，保留“观测 false-accept ≤ 预算”的最低门限；样本数小于 `min_samples` 的条件回退全局默认，并在 policy `notes` 里说明。报告同时给 coverage、selective accuracy、false-accept 和 ECE，不允许只报 accuracy。

v0.6.2 保留了可选的有限样本风险控制：

```bash
python scripts/fit_policy.py --input data/cal-temp/predictions.csv \
  --false-accept-budget 0.02 --risk-control wilson --delta 0.05 \
  --min-samples 20 --out data/acceptance-policy-v061.json
```

`risk-control=wilson` 不再比较观测 FAR，而是比较单侧 Wilson 上界与预算。40 个样本里 0 个误接受，观测 FAR 是 0%，但 95% 上界约 7%，所以不会假装“2% 预算已被证明”。这是保守的工程选择，不是精确 Clopper-Pearson 保证；论文里应写明近似和样本量。完整流程见 [`measurements/v0.4/README.md`](measurements/v0.4/README.md)，v0.6.2 的差异主要在 `fit_calibration.py --method beta` 和 `fit_policy.py --risk-control/--delta`。

本仓库的 85 张公开图结果：每条件 8 张标定 / 9 张测试，温度 `T=3.22`；测试集 coverage 28.9% → 35.6%，selective accuracy 100% → 93.8%，false accept 0% → 2.2%。完整数据见 [`measurements/v0.4/README.md`](measurements/v0.4/README.md)。

## 3. Modbus TCP 与联锁

`ModbusTcpServer` 是纯标准库的 Modbus TCP 子集（FC 1/3/4/5/6/15/16），默认监听 `127.0.0.1:15020`。寄存器映射：

| 地址 | 类型 | 含义 |
|---|---|---|
| coil 0 | coil | `alarm` |
| coil 1 | coil | `fail_safe` |
| coil 2 | coil | `interlock_ok` |
| coil 3 | coil | `heartbeat` |
| holding 0 | register | `class_id` |
| holding 1 | register | `confidence × 1000` |
| holding 2 | register | `latency_ms` |
| holding 3 | register | `state_code`（0 idle / 1 armed / 2 alarm / 3 fault） |
| holding 4 | register | `event seq` |
| holding 5 | register | `alarm_count` |

联锁规则：

- 连续 `assert_after` 帧检测到目标才置 alarm（默认 3）；
- 最后一帧检测后保持 `clear_after_s` 秒（默认 5），避免边界抖动；
- `stale_after_s` 内没有新样本，或 `watchdog_timeout_s` 内没有心跳 → `fault`，`fail_safe=true`；
- `fault` 锁存，必须 `reset` 命令才恢复；
- 输出带 `command_ack` / `capture_request`，PLC 可以确认命令。

启动：

```bash
python -m ecvision.edge --source synthetic --max-frames 300 \
  --interlock --modbus --modbus-port 15020 \
  --assert-after 3 --clear-after-s 5 --stale-after-s 2
```

用任意 Modbus 客户端读 `127.0.0.1:15020` 的 coil 0/1/2/3 和 holding 0..5。这个模拟器**不是**安全 PLC，不能替代真实安全回路；它用于验证协议、时序和降级逻辑。

## 4. 确定性激励台

`ecvision.bench` 生成距离/照度/模糊/遮挡的确定性矩阵：

```bash
python scripts/bench_stimulus.py \
  --out docs/measurements/stimulus.md \
  --repeats 3
```

有真实图片和 ground truth 时：

```bash
python scripts/bench_stimulus.py \
  --source-image samples/person.jpg --ground-truth person \
  --repeats 5 --out docs/measurements/stimulus-person.md
```

报告字段：`accept_rate`、`confidence_mean/std`、`latency_p50/p95`、`accuracy`。合成目标没有 ground truth，`accuracy` 为 `n/a`；这份报告只证明**时序稳定性和门限行为**，不证明识别精度。

## 5. SPC 漂移监控

`--spc` 开启 EWMA + 双侧 CUSUM（confidence、latency）和标签分布 JS 散度。告警进入运行摘要的 `spc.alerts`，不会中断主流程。

```bash
python -m ecvision.edge --source synthetic --max-frames 1000 --spc \
  --metrics-out data/spc.json
```

建议的判读：

- `ewma`：缓慢漂移（照度变化、镜头污染）；
- `cusum`：阶跃变化（目标类别变化、模型替换）；
- `js`：标签分布变化（场景切换或数据漂移）。

## 6. 事件时间与 watermark

`Event.ts` 是采集时间；Edge-Mem 另外记录 `ingest_time`：

- `capture_time`：端侧采集；
- `ingest_time`：网关收到并入库；
- `late`：事件时间早于 `watermark - ECV_MEMORY_LATE_SECONDS`；
- `watermark`：已见过的最大事件时间。

```bash
ECV_MEMORY_LATE_SECONDS=300
```

`/api/v1/memory/stats` 返回 `late_events`、`watermark`、`allowed_lateness_s`。晚到事件仍然入库并参与检索（安全场景不丢），但会被标记，便于后续做“是否允许影响实时告警”的策略。

## 7. 验收清单

```bash
# 1) 单测 + 静态检查
pytest -q
ruff check src tests scripts
ruff format --check src tests scripts

# 2) 时间链
python -m ecvision.edge --source synthetic --max-frames 200 \
  --clock-source ntp --clock-offset-ms 50 --deadline-ms 80 \
  --metrics-out data/clock.json

# 3) 标定（需要自备 JSONL）
python scripts/fit_calibration.py --input data/calibration.jsonl \
  --out data/calibration.json --conformal-out data/conformal.json

# 4) 控制闭环
python -m ecvision.edge --source synthetic --max-frames 300 \
  --interlock --modbus --modbus-port 15020

# 5) 激励台
python scripts/bench_stimulus.py --out docs/measurements/stimulus.md

# 6) 事件时间
pytest -q tests/test_event_time.py
```

## 8. 与测控课程的对应关系

| 课程概念 | 本项目落点 |
|---|---|
| 采样定理 / 采样周期 | `SamplingClock`、`capture_dt_us`、`deadline_ms` |
| 误差与不确定度 | 标定曲线、ECE/Brier、conformal set |
| 传感器标定 | `scripts/fit_calibration.py`、`CalibrationProfile` |
| 工业通信 | Modbus TCP 寄存器映射、命令 ACK |
| 闭环控制 | 联锁状态机、去抖、保持、fail-safe |
| 数据处理 | SPC（EWMA/CUSUM）、漂移告警 |
| 测试技术 | 确定性激励矩阵、重复性、p50/p95/p99 |

## 9. 参考

- ISO/IEC Guide 98-4:2012, *Uncertainty of measurement — Part 4: Role of measurement uncertainty in conformity assessment*.
- ISO 14253-1:2017, *Geometrical product specifications (GPS) — Inspection by measurement of workpieces and measuring equipment — Part 1: Decision rules for verifying conformity or nonconformity with specifications*.
- JCGM 101:2008, *Evaluation of measurement data — Supplement 1 to the GUM — Propagation of distributions using a Monte Carlo method*.
- Kull, M., Silva Filho, T. M., & Flach, P. (2017). Beta calibration: a well-founded and easily implemented improvement on logistic calibration for binary classifiers. AISTATS.
- Vovk, V., Gammerman, A., & Shafer, G. (2005). *Algorithmic Learning in a Random World*. Springer. (split conformal / Mondrian conformal)
- Angelopoulos, A. N., Bates, S., Candès, E. J., Jordan, M. I., & Lei, L. (2021). Learn then test: Calibrating predictive algorithms to achieve risk control. arXiv:2110.01052.
- Angelopoulos, A. N., Bates, S., Fisch, A., Lei, L., & Schuster, T. (2022). Conformal risk control. arXiv:2208.02814.

