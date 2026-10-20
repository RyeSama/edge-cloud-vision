# Edge-Mem：面向边缘时序事件的记忆与混合检索

Edge-Mem 是 v0.2.0 新增的模块。它不改变 v0.1.0 的“端侧过滤 + 云端兜底”主链路，而是在云端事件落库之后，回答一个更长期的问题：

> 过去发生了什么？现在能不能用自然语言把它找回来，并且只把必要的上下文交给 LLM？

它由四层组成：**SQLite 元数据 + 可选向量索引 + 时间衰减策略 + 有预算的上下文组装**。

---

## 1. 为什么不是“把所有事件塞进向量库”

工业时序事件有三个特点：

1. **写多读少**：每秒都有事件，但真正被检索的是少数。
2. **时间就是强约束**：用户问“昨天下午”，先做时间过滤比先做语义召回更便宜、更准。
3. **不能无限增长**：边缘网关的磁盘和内存都有限，原始明细必须有生命周期。

因此采用“**SQLite 做事实源，向量库做语义索引，时间窗做硬过滤**”的组合。向量库不是唯一存储；它挂了也不能影响事件入库和查询降级。

---

## 2. 三层存储与时间衰减

```text
              now-10min       now-2h              now
                 │              │                  │
  ───────────────┼──────────────┼──────────────────┼────────►
                 │    warm      │       hot        │
                 │  15-min      │   full detail    │
                 │  summaries   │                  │
  ───────────────┼──────────────┴──────────────────┘
        cold     │
   minimal index │
```

| 层级 | 时间范围 | 保留内容 | 查询方式 |
|---|---|---|---|
| hot | 最近 10 分钟 | 完整事件 JSON、决策、标签、置信度 | SQLite 时间过滤 + 向量 |
| warm | 10 分钟 – 2 小时 | 15 分钟粒度的聚合摘要（次数、平均/最大置信度、风险/动作计数） | SQLite + 摘要向量 |
| cold | 2 小时以上 | 仅 `event_id/device_id/ts/label/confidence/risk/action` 的极简索引 | 时间过滤；不进入语义索引 |

压缩由 `TemporalCompressor.compress(now)` 执行：

1. 把 `[now-2h, now-10min)` 的 hot 事件按 `(device_id, label, 15min bucket)` 聚合；
2. 写入 `summaries`，建立摘要向量，删除对应 raw 行；
3. 把早于 `now-2h` 的 raw 行写入 `cold_events`，删除 raw 行；
4. 返回可观测的 `CompressionReport`：生成摘要数、删除的 raw 行数、冷归档数、向量删除数、错误列表。

压缩是**有损**的，这是设计目标，不是缺陷。需要完整审计时，应在压缩前把 raw JSON 落到对象存储或数据湖；本项目只保留极简冷索引。

---

## 3. 时序 + 语义混合检索

查询流程：

```text
自然语言问题
   │
   ├─ 轻量时间解析（正则 + 中文日期/相对时间/时段）
   │      └─ TimeWindow(start, end)
   │
   ├─ SQLite 硬过滤：ts ∈ [start, end)，可选 device_id/label
   │
   ├─ 时间排序（越新越靠前） ──┐
   │                            ├─ RRF 融合 → top-k
   └─ 向量检索（在过滤后的 ID 集合内） ──┘
                                │
                                └─ ContextBuilder（Markdown 表格 / JSON 时间轴）
```

时间解析器覆盖：

- `2026-10-18`、`2026-10-18 14:30`
- `10月18日`
- `过去 30 分钟`、`最近 2 小时`、`last 3 days`
- `今天/昨天/前天 + 上午/下午/晚上`
- 无法识别时回退到最近 24 小时，并在 `TimeWindow.source="fallback"` 中标记

### RRF 公式

对每个候选事件 `d`：

```text
score(d) = w_time     / (k + rank_time(d))
         + w_semantic / (k + rank_semantic(d))
```

默认 `k=60`、`w_time=0.4`、`w_semantic=0.6`。只有同时出现在两个榜单里的候选，才会拿到两份分数；只在时间榜里的候选仍然保留，避免“语义索引没命中就什么都查不到”。

`rrf_fuse()` 是纯函数，单元测试直接验证权重和排序，不依赖数据库或 embedding 模型。

### 为什么先硬过滤再向量检索

- 先把候选集从“全库”缩到“时间窗内”，向量比较的基数更小；
- 时间窗是用户问题里最明确的约束，先应用它不会误伤语义召回；
- 过滤后的 ID 集合传给向量库的 `allowed_ids`，避免把窗口外的事件重新召回。

---

## 4. 上下文组装与 Token 预算

`ContextBuilder` 把检索结果按时间升序排列（时间轴因果关系），输出：

- **Markdown 表格**：适合直接拼进 LLM prompt；
- **JSON 时间轴**：没有 LLM 时的原始返回，也方便程序消费。

Token 估算采用保守规则：CJK 字符按 1 token，ASCII 按约 4 字符/token，再向上取整。它不是某个具体 tokenizer 的精确计数，但能保证“不超过预算”这个工程约束；需要精确计费时，应在网关侧换用模型对应的 tokenizer。

预算控制是**逐行加入、超预算即停**，并在 `ContextBundle.truncated` 中标记。不会出现“先拼一个超大字符串再截断”的不可控行为。

---

## 5. HTTP API

启动网关后：

```bash
python -m ecvision.cloud
# http://127.0.0.1:8000/docs
```

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/memory/stats` | 各层事件数、向量数、向量后端健康状态，以及 `late_events` / `watermark` / `allowed_lateness_s` |
| POST | `/api/v1/memory/query` | 混合检索，返回窗口、排序后的条目和上下文 |
| POST | `/api/v1/memory/ask` | 检索 + 可选 LLM 回答；无 LLM 时返回 JSON 时间轴 |
| POST | `/api/v1/memory/compress` | 手动触发一次时间衰减压缩 |

查询示例：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/memory/query \
  -H 'Content-Type: application/json' \
  -d '{
    "question": "昨天下午穿红衣服的人是什么时候出现的？",
    "limit": 10,
    "token_budget": 2000,
    "format": "markdown"
  }'
```

返回结构的关键字段：

```json
{
  "question": "...",
  "window": {"start": "...", "end": "...", "label": "昨天下午", "source": "relative"},
  "items": [
    {"event_id": "...", "ts": "...", "tier": "warm", "score": 0.0163,
     "time_rank": 2, "semantic_rank": 1, "summary_count": 50}
  ],
  "context": {"format": "markdown", "text": "...", "token_count": 812,
              "truncated": false, "item_count": 10},
  "degraded": false,
  "warnings": [],
  "total_candidates": 37,
  "retrieval_ms": 4.2
}
```

`/api/v1/memory/ask` 在未配置 `ECV_LLM_API_KEY` 时返回：

```json
{"answer": null, "mode": "json_timeline", "context": {"format": "json", "...": "..."}}
```

这不是错误，而是明确的降级模式；调用方不需要解析异常。

---

## 6. 事件时间与晚到数据

v0.3 把 `Event.ts` 明确为**采集时间**，Edge-Mem 另外记录入库时间：

- `capture_time`：端侧采集时刻；
- `ingest_time`：网关收到并写入 SQLite 的时刻；
- `watermark`：已见过的最大 `capture_time`；
- `late`：`capture_time < watermark - allowed_lateness_s`。

晚到事件**不丢弃**，仍然入库并参与检索，只标记 `late=true`。原因和安全场景一致：丢一条晚到事件可能比多一条重复更危险。需要“晚到数据不得影响实时告警”时，由上层策略读取 `late` 字段决定，而不是在存储层静默过滤。

```bash
ECV_MEMORY_LATE_SECONDS=300 python -m ecvision.cloud
```

`MemoryStats` 返回：

```json
{"events": 3, "late_events": 1, "watermark": "2026-10-19T09:30:00+00:00", "allowed_lateness_s": 300}
```

---

## 7. 降级与可观测性

| 故障 | 行为 | 可观测性 |
|---|---|---|
| 向量库初始化失败 | 自动回退到进程内向量索引 | `/healthz.memory_warnings`、`vector_backend` |
| 向量检索抛异常 | 回退到 SQLite 文本/标签过滤，`degraded=true` | `warnings`、`last_vector_error` |
| Chroma 未安装 | `build_vector_index` 返回 warning，使用内存索引 | `memory_warnings` |
| LLM 未配置 | `/ask` 返回 `json_timeline` | `mode` |
| LLM 调用失败 | `/ask` 返回 `degraded`，保留检索时间轴 | `mode`、`warnings` |
| SQLite 初始化失败 | v0.1 事件主链路继续，memory API 返回 503 | `/healthz.memory_error` |
| 压缩时向量删除失败 | 记录到 `CompressionReport.errors`，继续压缩 | `errors` |

原则和 v0.1 一样：**降级要可见，不能假装成功**。

---

## 8. 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ECV_MEMORY_DB_PATH` | `data/edge_mem.sqlite3` | SQLite 路径；`:memory:` 可用于测试 |
| `ECV_MEMORY_TIMEZONE` | `Asia/Shanghai` | 时间解析的本地时区 |
| `ECV_MEMORY_HOT_MINUTES` | `10` | hot 明细窗口 |
| `ECV_MEMORY_WARM_HOURS` | `2` | warm 聚合窗口 |
| `ECV_MEMORY_BUCKET_MINUTES` | `15` | 聚合桶大小 |
| `ECV_MEMORY_TOKEN_BUDGET` | `4000` | 默认上下文预算 |
| `ECV_MEMORY_VECTOR_BACKEND` | `memory` | `memory` 或 `chroma` |
| `ECV_MEMORY_CHROMA_PATH` | `data/chroma` | Chroma 持久化目录 |
| `ECV_MEMORY_VECTOR_DIM` | `256` | 哈希 embedding 维度 |
| `ECV_MEMORY_AUTO_COMPRESS` | `1` | 启动时是否执行一次压缩 |
| `ECV_MEMORY_LATE_SECONDS` | `300` | 允许迟到窗口；超过则标记 `late`，不丢弃 |

启用 Chroma：

```bash
pip install -e ".[memory]"
ECV_MEMORY_VECTOR_BACKEND=chroma python -m ecvision.cloud
```

默认的 `memory` 后端不依赖任何外部服务；进程重启后由 SQLite 中的事件/摘要文本重建索引（`MemoryService.reindex()`），所以不会因为向量索引丢失而无法查询。FAISS 没有直接内置，但实现 `VectorIndex` 的 `upsert/query/delete/count/healthy` 五个方法即可接入，不需要改检索器。

---

## 9. 取舍与限制

- **哈希 embedding 不是语义模型。** 它用字符 n-gram 做确定性向量化，保证离线可跑、测试可复现；真实语义检索应替换为 `Embedder` 的模型实现（如 bge-small / text-embedding-3-small），接口不变。
- **时间解析不是通用 NLP。** 只覆盖项目实际会遇到的日期/相对时间/时段表达；更复杂的表达应接入专门的时间解析库或 LLM 抽取，并保留 `fallback` 标记。
- **冷层不进入语义索引。** 冷归档只有极简索引，语义检索只覆盖 hot/warm；这是有意的成本取舍。
- **压缩是破坏性的。** 需要完整审计时，先落对象存储/数据湖，再执行压缩。
- **单进程 SQLite。** 适合单网关/演示；多实例并发写应换 PostgreSQL/TimescaleDB，并把向量库独立部署。
- **Token 估算是近似值。** 精确预算应使用目标模型的 tokenizer。

---

## 10. 与 C++/MCU 的关系

Edge-Mem 本身运行在边缘网关（Python/FastAPI），不要求 MCU 实现向量检索。MCU 侧仍然只负责 v0.1 的采集、轻量推理和 JSON 发布；迁移边界没有变化。

如果未来要把“记忆”下沉到网关的 C++ 服务，可复用的契约是：

- `MemoryItem` 的字段与 JSON 表达；
- 时间窗的 UTC 半开区间语义；
- RRF 的权重与 `k` 参数；
- `CompressionReport` 的可观测字段。

SQLite、Chroma 和 Python 实现都是可替换的适配器。
