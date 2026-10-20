# 平面视觉测量：从像素到毫米

v0.5 加的不是又一个识别模块，而是一条几何测量链：

```text
棋盘格标定 → 相机内参/畸变 → 平面位姿 solvePnP
           → 像素反投影成射线 → 与平面求交 → 两点距离(mm)
           → 蒙特卡洛不确定度 → Gauge R&R
```

它回答的是“这个零件在两帧之间移动了多少毫米”，而不是“这张图里像不像人”。两者不能互相替代。

## 1. 能测什么，不能测什么

能测：

- 与标定板共面的两个点之间的距离；
- 同一平面上的孔距、边距、位移；
- 重复测量的均值、标准差、95% 区间；
- 测量系统的重复性、再现性和 GRR/tolerance。

不能测：

- 不共面的目标（需要双目、结构光或深度相机）；
- 亚像素边缘之外的精度；
- 温度漂移、镜头畸变随变焦变化；
- 没有溯源基准的“绝对准确度”。

这套实现只做平面测量。平面假设必须写进报告，不能默认成立。

## 2. 标定

用内角点 `9×6`、方格 `25 mm` 的棋盘格，拍 12–20 个不同姿态。要求：

- 棋盘格平整，打印后贴在硬板上；
- 方格实际尺寸用卡尺抽测，不用标称值；
- 覆盖画面的四角和中心，姿态有倾斜，但不要极端；
- 对焦、变焦、分辨率在标定和测量时保持一致；
- 避免反光、运动模糊。

`scripts/calibrate_camera.py` 会输出 `CalibrationResult`，包含：

- `fx, fy, cx, cy`；
- 5 个畸变参数；
- 每个视图的重投影 RMS；
- 总体 RMS。

经验门槛：RMS 小于 0.5 px 才能用于毫米级测量；大于 1 px 先查角点检测和棋盘格平整度，不要直接测。

## 3. 平面测量

测量时，棋盘格和被测对象放在同一平面上。先检测棋盘格角点，用 `solvePnP` 求平面位姿，再把两个像素反投影成射线，与平面求交，最后算两点距离。

```bash
python scripts/calibrate_camera.py \
  --images "data/calib/*.jpg" --cols 9 --rows 6 --square-mm 25 \
  --out data/camera.json

python scripts/measure_checkerboard.py \
  --camera data/camera.json --image data/measure/scene.jpg \
  --cols 9 --rows 6 --square-mm 25 \
  --a 412,308 --b 690,312 --out data/measure/result.json
```

`--a/--b` 是图像像素坐标。实际使用时，可以用 OpenCV 窗口点选，或先用边缘检测/模板匹配拿到两个点，再传给脚本。

## 4. 不确定度

`PlanarMeasurement.measure_with_uncertainty()` 做蒙特卡洛传播：

1. 给棋盘格角点和两个测量点加像素噪声（默认 σ=0.5 px）；
2. 每次重新 `solvePnP`，重新反投影、求交；
3. 对距离乘一个标定尺度扰动（默认 0.1%）；
4. 取中位数、标准差和 2.5/97.5 分位。

报告里还会单独列 GUM 风格的预算：

| 来源 | 典型值 | 说明 |
|---|---:|---|
| 像素定位 + 平面位姿 | 由蒙特卡洛给出 | 角点噪声、姿态耦合 |
| 标定尺度 | 0.1% | 棋盘格尺寸和标定误差 |
| 靶面平整度/放置 | 0.03 mm | 假设值，真实项目要测 |

合成验证结果：100 mm 真值，测得 100.090 mm，12 次重复误差均值 0.0175 mm、RMS 0.0696 mm；合成标准不确定度 0.6185 mm，U(k=2)=1.2369 mm。误差小于 U，说明区间是保守的，不是“精度 0.07 mm”的广告。

## 5. Gauge R&R

`analyze_gauge()` 支持多零件、多操作员、多次试验：

- 重复性：同一零件/操作员内方差的合并；
- 再现性：操作员均值之间的方差；
- GRR：两者平方和开根；
- `%GRR = 6·GRR / tolerance`；
- `ndc = 1.41·PV/GRR`。

合成研究：5 个零件、2 个操作员、3 次试验，重复性 0.0097 mm，再现性 0.0209 mm，GRR 0.0230 mm，公差 1 mm 时 `%GRR=13.79%`，`ndc=9`。

这套数字只验证估计器；真实项目要换真实零件、真实操作员、真实装夹。

## 6. 合成验证与真实测量的区别

`scripts/metrology_demo.py` 用已知内参的合成针孔相机生成棋盘格和测量点。它验证的是：

- 投影/反投影是否自洽；
- `solvePnP` 和平面求交是否正确；
- 不确定度传播是否随噪声增大；
- 报告和 Gauge 计算是否跑通。

它不能验证：

- 真实镜头的畸变模型是否够用；
- 棋盘格打印/粘贴是否平整；
- 实际光照、反光、景深；
- 相机的热漂移和长期稳定性。

所以合成结果只能写“方法验证”，不能写“测量精度 0.07 mm”。真实精度必须用卡尺/量块/三坐标做对照。

## 7. 文件

- `src/ecvision/metrology/camera.py`：内参、投影、反投影射线；
- `src/ecvision/metrology/calibration.py`：棋盘格检测与 `calibrateCamera`；
- `src/ecvision/metrology/measurement.py`：平面位姿、求交、蒙特卡洛；
- `src/ecvision/metrology/uncertainty.py`：GUM 合成与预算表；
- `src/ecvision/metrology/gauge.py`：Gauge R&R；
- `src/ecvision/metrology/report.py`：Markdown 报告；
- `src/ecvision/metrology/synthetic.py`：合成场景；
- `scripts/calibrate_camera.py`、`scripts/measure_checkerboard.py`、`scripts/metrology_demo.py`。

## 8. 限制

- 只支持平面目标；
- 只支持针孔 + 5 参数畸变，不支持鱼眼/卷帘快门建模；
- 棋盘格角点检测依赖 OpenCV，强反光/低对比会失败；
- 不确定度里的“靶面平整度”是假设值，不是实测；
- 没有温度、振动、装夹重复性的长期数据；
- 没有做量值溯源，不能声称校准证书级别的准确度。

这些限制不影响它作为一个“可验证的测量链”作品，但写进复试/论文时必须说清楚。
