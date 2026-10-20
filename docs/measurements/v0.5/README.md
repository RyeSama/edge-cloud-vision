# v0.5 平面测量验证

这是 `scripts/metrology_demo.py` 的合成验证结果，不是真实相机精度。

## 方法

- 合成针孔相机：1280×720，fx=fy=900 px，cx=640，cy=360，5 参数畸变；
- 15 个棋盘格视图，9×6 内角点，方格 25 mm，角点噪声 σ=0.1 px；
- 测量点：靶面上 (50,50) 到 (150,50)，真值 100 mm；
- 12 次重复场景，每次 160 个蒙特卡洛样本；
- Gauge R&R：5 零件 × 2 操作员 × 3 次，公差 1 mm。

复现：

```bash
python scripts/metrology_demo.py --out docs/measurements/v0.5
```

## 结果

| 指标 | 数值 |
|---|---:|
| 标定 RMS 重投影 | 0.1384 px |
| 拟合 fx / fy | 906.63 / 907.02 px（真值 900） |
| 拟合 cx / cy | 646.12 / 361.57 px（真值 640 / 360） |
| 测量值 | 100.0899 mm |
| 12 次误差均值 | 0.0175 mm |
| 12 次误差 RMS | 0.0696 mm |
| 单次标准不确定度 | 0.6096 mm |
| 合成标准不确定度 | 0.6185 mm |
| 扩展不确定度 U(k=2) | 1.2369 mm |
| Gauge 重复性 | 0.0097 mm |
| Gauge 再现性 | 0.0209 mm |
| Gauge GRR | 0.0230 mm |
| %GRR / 1 mm 公差 | 13.79% |
| ndc | 9 |

## 怎么解读

- 误差 RMS 0.07 mm 小于 U=1.24 mm，说明不确定度区间是保守的；不要把它读成“精度 0.07 mm”。
- 拟合 fx 与真值差 0.74%，属于合成角点噪声和视图姿态覆盖的联合影响；真实相机还要受镜头畸变模型误差影响。
- Gauge 数字来自另一组合成测量值，用来验证估计器，不是上面相机测量的重复性。

## 文件

- `camera.json`：标定结果；
- `calibration-summary.json`：真值/拟合内参和每视图 RMS；
- `measurements.csv`：12 次重复测量；
- `uncertainty-budget.csv`：不确定度预算；
- `gauge.csv`、`gauge-summary.json`：Gauge 数据与结果；
- `summary.json`：机器可读汇总；
- `report.md`：Markdown 报告。
