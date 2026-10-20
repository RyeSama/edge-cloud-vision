# v0.4 标定与分条件验收

这组数据回答一个问题：**同一个模型，在模糊、遮挡、低照度和降采样下，应该用同一个 0.90 门限吗？**

数据集是 v0.3 验证用的 17 类 × 5 条件 = 85 张 ImageNet 样本图（每类每条件 1 张）。按类别做 group-aware 切分：8 类 / 40 张用于标定，9 类 / 45 张用于测试，测试类别不参与阈值选择。

> 这是小样本工程试验，不是工业现场精度声明。每个条件在标定集只有 8 张、测试集只有 9 张；阈值和 coverage 的方差都很大，结论应理解为“方法可行”，而不是“参数已定”。

## 流程

1. `scripts/split_manifest.py` 按 `condition_id` 前缀分组，避免同一类出现在两边；
2. `scripts/eval_dataset.py` 在标定集上跑原始置信度；
3. `scripts/fit_calibration.py --method temperature` 拟合温度标定；
4. `scripts/fit_policy.py` 在标定集上按 false-accept 预算选择每个条件的 accept 门限；
5. `scripts/eval_dataset.py --policy` 在独立测试集上验收。

## 结果

标定集 40 张、测试集 45 张，温度标定 `T=3.22`。

| 方案 | 测试 accuracy | coverage | selective accuracy | false accept rate | ECE |
|---|---:|---:|---:|---:|---:|
| 原始置信度，全局 0.90 | 0.667 | 0.289 | 1.000 | 0.000 | 0.267 |
| 温度标定，全局 0.90 | 0.667 | 0.000 | n/a | 0.000 | 0.291 |
| 温度标定 + 分条件策略 | 0.667 | 0.356 | 0.938 | 0.022 | 0.291 |

读法：

- 只做温度标定、仍用 0.90，会把所有测试样本都推入 review——标定降低了过度自信，但单一高门限没有考虑条件差异；
- 分条件策略把 coverage 从 28.9% 提到 35.6%，代价是 2.2% 的 false accept（16 个 accept 中 1 个错）；
- ECE 在测试集上反而比标定集高（0.291 vs 0.158），说明类别/条件偏移下标定不是万能药；报告必须同时给 coverage 和 false accept，不能只报 accuracy。

## 标定集上选出的门限

| 条件 | accept threshold | 标定 n | 标定 coverage | 标定 false accept |
|---|---:|---:|---:|---:|
| clean | 0.515 | 8 | 0.500 | 0.000 |
| blur | 0.660 | 8 | 0.250 | 0.000 |
| occlusion | 0.593 | 8 | 0.375 | 0.000 |
| lowlight | 0.663 | 8 | 0.375 | 0.000 |
| downscale | 0.771 | 8 | 0.250 | 0.000 |
| default | 0.496 | 40 | 0.425 | 0.050 |

模糊和降采样需要更高门限，clean 可以更低；这正是“分条件验收”想表达的东西。

## 复现

```bash
python scripts/split_manifest.py --input manifest.csv \
  --calibration-out manifest-cal.csv --test-out manifest-test.csv \
  --calibration-fraction 0.5 --seed 7

python scripts/eval_dataset.py --manifest manifest-cal.csv --out-dir out/cal-raw
python scripts/fit_calibration.py --input out/cal-raw/predictions.csv \
  --method temperature --out data/calibration-v04.json
python scripts/eval_dataset.py --manifest manifest-cal.csv --out-dir out/cal-temp \
  --calibration-profile data/calibration-v04.json
python scripts/fit_policy.py --input out/cal-temp/predictions.csv \
  --false-accept-budget 0.05 --min-samples 8 --out data/acceptance-policy-v04.json
python scripts/eval_dataset.py --manifest manifest-test.csv --out-dir out/test-policy \
  --calibration-profile data/calibration-v04.json \
  --policy data/acceptance-policy-v04.json
```

原始输出在本目录：

- `calibration-v04.json`（温度 `T=3.22`）
- `acceptance-policy-v04.json`
- `calibration-raw-summary.json` / `calibration-temp-summary.json`
- `test-raw-summary.json` / `test-global-summary.json` / `test-policy-summary.json`
- `test-policy-report.md` / `test-policy-predictions.csv`

## 边界

- 85 张图来自公开 ImageNet 样本，不是工业缺陷/安全场景数据；
- 每条件 8 张标定样本，选出的门限只能作为工程起点，不能作为出厂参数；
- 温度标定是二分类置信度校准，不是多分类 softmax 重标定；
- false-accept 预算 0.05 是人为约束，生产要按漏报代价重新定；
- 本目录只证明流程和报告口径，不宣称模型精度或安全认证。
