# 数据集验证报告

- 模型：`mobilenet_v1_1.0_224_quant`
- 样本数：45
- 全局门限（policy 可按条件覆盖）：accept ≥ 0.9，review ≥ 0.5
- 验收策略：`acceptance-policy-v04.json`，false-accept 预算 ≤ 0.05，min_samples=8

## 总览

| 指标 | 数值 | 说明 |
|---|---:|---|
| top-1 accuracy | 0.666667 | 全部样本 |
| coverage（accept） | 0.355556 | 被直接接受的比例 |
| review rate | 0.644444 | 进入复核的比例 |
| selective accuracy | 0.9375 | accept 中正确的比例 |
| false accept rate | 0.022222 | accept 且错误 / 全部 |
| false accept among accepted | 0.0625 | accept 中错误比例 |
| ECE（10 箱） | 0.29054 | 越低越好 |
| Brier | 0.243827 | 越低越好 |
| conformal coverage | — | 无 conformal 时为 null |
| latency p50 / p95 / p99 | 11.796 / 16.886 / 19.263 ms | 预处理+推理，不含采集等待 |

> accuracy 的分母是全部样本；coverage 的分母也是全部样本。不要把 selective accuracy 当 accuracy 报。

## 分条件结果

| distance_m | lux | degradation | n | accuracy | coverage | mean confidence | accept_threshold |
|---:|---:|---|---:|---:|---:|---:|---:|
|  | 500 | blur | 9 | 0.555556 | 0.333333 | 0.364532 | 0.660145 |
|  | 500 | clean | 9 | 0.888889 | 0.666667 | 0.630556 | 0.514596 |
|  | 500 | downscale | 9 | 0.555556 | 0.111111 | 0.526045 | 0.771386 |
|  | 500 | occlusion | 9 | 0.555556 | 0.222222 | 0.483481 | 0.592529 |
|  | 80 | lowlight | 9 | 0.777778 | 0.444444 | 0.574234 | 0.663072 |

## 验收策略（在标定集上选择）

| condition | accept_threshold | n | coverage | false accept rate | source |
|---|---:|---:|---:|---:|---|
| blur | 0.660145 | 8 | 0.25 | 0.0 | fitted |
| clean | 0.514596 | 8 | 0.5 | 0.0 | fitted |
| default | 0.49636 | 40 | 0.425 | 0.05 | default |
| downscale | 0.771386 | 8 | 0.25 | 0.0 | fitted |
| lowlight | 0.663072 | 8 | 0.375 | 0.0 | fitted |
| occlusion | 0.592529 | 8 | 0.375 | 0.0 | fitted |

## 混淆（truth → predicted）

| truth | predicted | count |
|---|---|---:|
| Persian cat | Persian cat | 2 |
| Persian cat | tiger cat | 2 |
| Persian cat | llama | 1 |
| Samoyed | Samoyed | 5 |
| car wheel | car wheel | 5 |
| cellular telephone | hand-held computer | 3 |
| cellular telephone | cellular telephone | 2 |
| coffee mug | coffee mug | 5 |
| desk | desk | 3 |
| desk | barbell | 1 |
| desk | shoe shop | 1 |
| electric fan | electric fan | 2 |
| electric fan | tape player | 2 |
| electric fan | microwave | 1 |
| laptop | laptop | 2 |
| laptop | potter's wheel | 2 |
| laptop | notebook | 1 |
| sports car | sports car | 4 |
| sports car | convertible | 1 |

## 口径与边界

- 本报告只评估 gate 行为与标定，不证明模型在工业现场的泛化精度。
- 延迟是 `preprocess_ms + inference_ms`，不含相机曝光、USB、MQTT、网络。
- 样本量小的时候，ECE 和 coverage 的方差很大；请同时看每箱样本数。
