# v0.5 planar metrology validation (synthetic camera)

## Camera

| 项目 | 数值 |
|---|---:|
| 分辨率 | 1280 × 720 px |
| fx / fy | 906.629 / 907.019 px |
| cx / cy | 646.117 / 361.571 px |
| 畸变参数 | -0.117620, -0.222964, 0.001712, 0.001107, 2.868837 |
| 标定视图 / 点 | 15 / 810 |
| RMS 重投影误差 | 0.1384 px |

## Planar measurement

| 项目 | 数值 |
|---|---:|
| 测量值 | 100.0899 mm |
| 标准不确定度 | 0.6096 mm |
| 95% 区间 | 98.8352 – 101.4336 mm |
| 蒙特卡洛样本 | 160 |
| 像素定位 σ | 0.500 px |
| 标定尺度 σ | 0.100% |

## Uncertainty budget

| 来源 | 标准不确定度 / mm | 灵敏度 | 贡献 / mm | 占比 |
|---|---:|---:|---:|---:|
| pixel localization + plane pose | 0.6096 | 1.000 | 0.6096 | 97.2% |
| calibration scale | 0.1000 | 1.000 | 0.1000 | 2.6% |
| target flatness / placement | 0.0300 | 1.000 | 0.0300 | 0.2% |
| combined | 0.6185 | 1.000 | 0.6185 | 100.0% |

合成标准不确定度 **0.6185 mm**，扩展不确定度 **U(k=2) = 1.2369 mm**。

## Gauge R&R

| 指标 | 数值 |
|---|---:|
| 零件 / 操作员 / 试验 | 5 / 2 / 3 |
| 重复性 | 0.0097 mm |
| 再现性 | 0.0209 mm |
| GRR | 0.0230 mm |
| ndc | 9 |
| %GRR / 公差 | 13.79% |

## Notes

- Synthetic pinhole camera: ground truth fx/fy/cx/cy is known; this validates projection, pose, back-projection and uncertainty propagation.
- The repeatability column is the spread of repeated scene noise, not a physical repeatability study.
- Repeat error mean = 0.0175 mm, RMS = 0.0696 mm.
- Real deployment needs real checkerboard images, a flat target, and a traceable reference (caliper/gauge block).
- The Gauge R&R study uses its own synthetic measurement values to exercise the estimator; it is not the camera run above.
